"""
    DC3-MWCP framework primary object used for execution of parsers and collection of metadata
"""
from __future__ import print_function

import contextlib

from future.builtins import str, open, map

import base64
import binascii
import hashlib
import json
import ntpath
import logging
import os
import re
import shutil
import sys
import tempfile
import codecs

import mwcp
import mwcp.parsers
from mwcp import config
from mwcp.utils.stringutils import convert_to_unicode
from mwcp.utils import logutil


logger = logging.getLogger(__name__)
ascii_writer = codecs.getwriter('ascii')

PY3 = sys.version_info > (3,)

# pefile is now strictly optional, loaded down below so we can use
# reporter for error reporting

INFO_FIELD_ORDER = ['inputfilename', 'md5', 'sha1', 'sha256', 'compiletime']
STANDARD_FIELD_ORDER = ["c2_url", "c2_socketaddress", "c2_address",
                        "proxy", "proxy_credential", "proxy_username", "proxy_password",
                        "proxy_socketaddress", "proxy_address", "proxyport",
                        "url", "urlpath",
                        "socketaddress", "address", "port", "listenport",
                        "credential", "username", "password",
                        "missionid", "useragent", "interval", "version", "mutex",
                        "service", "servicename", "servicedisplayname", "servicedescription",
                        "serviceimage", "servicedll", "injectionprocess",
                        "filepath", "directory", "filename",
                        "registrykeyvalue", "registrykey", "registryvalue", "key"]


class ReporterLogHandler(logging.Handler):
    """Custom logging handler used to keep backwards compatible with legacy logging mechanism."""

    def __init__(self, reporter):
        super(ReporterLogHandler, self).__init__()
        self._reporter = reporter

    def emit(self, record):
        message = self.format(record)
        if record.levelno > logging.WARNING:
            self._reporter.errors.append(message)
        # Even though reporter uses the name "debug".. This really is an INFO level debug message.
        # (Adding true DEBUG level messages would spam our console.)
        elif logging.INFO <= record.levelno <= logging.WARNING:
            self._reporter.add_metadata("debug", message)


class Reporter(object):
    """
    Class for doing heavy lifting of parser execution and metadata reporting

    This class contains state and data about the current config parsing run, including extracted
    metadata, holding the actual sample, etc.

    Re-using an instance of this class on multiple samples is possible and should be safe, but it
    is not recommended

    Attributes:
        tempdir:
            directory where temporary files should be created. Files created in this directory should
            be deleted by parser. See managed_tempdir for mwcp managed directory
        input_file:
            the original parsed file. (instance of mwcp.FileObject)
        metadata:
            Dictionary containing the metadata extracted from the malware by the parser
        outputfiles:
            dictionary of entries for each output file. The key is the filename specified. Each entry
            is a dictionary with keys of data, description, and md5. If the path key is set, the file was written
            to that path on the filesystem.
        fields:
            dictionary containing the standardized fields with each field comprising an embedded
            dictionary. The 1st level keys are the field names. Under that, the keys are "description",
            "examples", and "type". See fields.json.
        errors:
            list of errors generated by framework. Generally parsers should not set these, they should
            use debug instead

    """
    URL_RE = re.compile(r"[a-z\.-]{1,40}://(?P<address>\[?[^/]+\]?)(?P<path>/[^?]+)?")
    PORT_RE = re.compile(r"[0-9]{1,5}")
    SHA1_RE = re.compile('[0-9a-fA-F]{40}')

    def __init__(self,
                 outputdir=None,
                 tempdir=None,
                 outputfile_prefix=None,
                 disable_output_files=False,
                 disable_temp_cleanup=False,
                 base64_output_files=False,
                 ):
        """
        Initializes the Reporter object

        :param str tempdir: sets path to temporary directory
        :param str outputdir:
            sets directory for output_file(). Should not be written to (or read from) by parsers
            directly (use tempdir)
        :param str outputfile_prefix:
            sets prefix for output files written to outputdir. Special value "md5" causes prefix
            by md5 of the input file.
        :param bool disable_output_files: disable writing if files to filesystem
        :param bool disable_temp_cleanup: disable cleanup (deletion) of temp files
        """

        # defaults
        self.tempdir = tempdir or tempfile.gettempdir()
        self.outputfiles = {}
        self._log_handler = None
        self.fields = {"debug": {"description": "debug", "type": "listofstrings"}}
        self.metadata = {}
        self.errors = []
        self.input_file = None

        self._managed_tempdir = None
        self._output_dir = outputdir or ''
        self._output_file_prefix = outputfile_prefix or ''

        self._disable_output_files = disable_output_files
        self._disable_temp_cleanup = disable_temp_cleanup
        self._base64_output_files = base64_output_files

        with open(config.FIELDS_PATH, 'rb') as f:
            self.fields = json.load(f)

    @property
    def managed_tempdir(self):
        """
        Returns the filename of a managed temporary directory. This directory will be deleted when
        parser is finished, unless tempcleanup is disabled.
        """

        if not self._managed_tempdir:
            self._managed_tempdir = tempfile.mkdtemp(
                dir=self.tempdir, prefix="mwcp-managed_tempdir-")

            if self._disable_temp_cleanup:
                logger.info("Using managed temp dir: {}".format(self._managed_tempdir))

        return self._managed_tempdir

    def _add_metatadata_listofstrings(self, key, value):
        if not value:
            logger.info("no values provided for {}, skipping".format(key))
            return
        value = convert_to_unicode(value)
        obj = self.metadata.setdefault(key, [])
        if key == 'debug' or value not in obj:
            obj.append(value)

        if key == "filepath":
            # use ntpath instead of os.path so we are consistent across platforms. ntpath
            # should work for both windows and unix paths. os.path works for the platform
            # you are running on, not necessarily what the malware was written for.
            # Ex. when running mwcp on linux to process windows
            # malware, os.path will fail due to not handling
            # backslashes correctly.
            self.add_metadata("filename", ntpath.basename(value))
            self.add_metadata("directory", ntpath.dirname(value))

        if key == "c2_url":
            self.add_metadata("url", value)

        if key in ("c2_address", "proxy_address"):
            self.add_metadata("address", value)

        if key == "serviceimage":
            # we use tactic of looking for first .exe in value. This is
            # not guaranteed to be reliable
            if '.exe' in value:
                self.add_metadata("filepath", value[
                                              0:value.find('.exe') + 4])

        if key == "servicedll":
            self.add_metadata("filepath", value)

        if key == "ssl_cer_sha1":
            if not self.SHA1_RE.match(value):
                logger.error("Invalid SHA1 hash found: {!r}".format(value))

        if key in ("url", "c2_url"):
            # http://[fe80::20c:1234:5678:9abc]:80/badness
            # http://bad.com:80
            # ftp://127.0.0.1/really/bad?hostname=pwned
            match = self.URL_RE.search(value)
            if not match:
                logger.error("Error parsing as url: %s" % value)
                return

            if match.group("path"):
                self.add_metadata("urlpath", match.group("path"))

            if match.group("address"):
                address = match.group("address").rstrip(': ')
                if address.startswith("["):
                    # ipv6--something like
                    # [fe80::20c:1234:5678:9abc]:80
                    domain, found, port = address[1:].partition(']:')
                else:
                    domain, found, port = address.partition(":")

                if found:
                    if port:
                        if key == "c2_url":
                            self.add_metadata("c2_socketaddress", [domain, port, "tcp"])
                        else:
                            self.add_metadata("socketaddress", [domain, port, "tcp"])
                    else:
                        logger.error("Invalid URL {!r} found ':' at end without a port.".format(address))
                else:
                    if key == "c2_url":
                        self.add_metadata("c2_address", address)
                    else:
                        self.add_metadata("address", address)

    def _add_metadata_listofstringtuples(self, key, values):
        # Pad values that allow for shorter versions.
        expected_size = {
            'proxy': 5,
            'rsa_private_key': 8,
        }
        if key in expected_size:
            values = tuple(values) + ('',) * (expected_size[key] - len(values))

        values = list(map(convert_to_unicode, values))

        obj = self.metadata.setdefault(key, [])
        if values not in obj:
            obj.append(values)

        # Add subfield components.
        subfield_map = {
            "registrypathdata": ["registrypath", "registrydata"],
            "service": ["servicename", "servicedisplayname", "servicedescription", "serviceimage", "servicedll"],
            "credential": ["username", "password"],
        }
        if key in subfield_map:
            subfields = subfield_map[key]
            for subfield, _value in zip(subfields, values):
                if _value:
                    self.add_metadata(subfield, _value)
            if len(values) != len(subfields):
                logger.warn("Expected {} values in type {}, received {}".format(len(subfields), key, len(values)))

        # Special case validation.
        if key == "c2_socketaddress":
            self.add_metadata("socketaddress", values)
            self.add_metadata("c2_address", values[0])

        if key == "proxy_socketaddress":
            self.add_metadata("socketaddress", values)
            self.add_metadata("proxy_address", values[0])

        if key == "socketaddress":
            if len(values) != 3:
                logger.warn(
                    "Expected three values in type socketaddress, received %i" % len(values))
            self.add_metadata("address", values[0])
            self.add_metadata("port", values[1:])

        if key in ("port", "listenport"):
            if len(values) != 2:
                logger.warn("Expected two values in type %s, received %i" % (
                    key, len(values)))
            # check for integer number and valid proto?
            match = self.PORT_RE.search(values[0])
            if match:
                portnum = int(values[0])
                if portnum < 0 or portnum > 65535:
                    logger.warn(
                        "Expected port to be number between 0 and 65535")
            else:
                logger.warn(
                    "Expected port to be number between 0 and 65535")
            if len(values) >= 2:
                if values[1] not in ["tcp", "udp", "icmp"]:
                    logger.warn(
                        "Expected port type to be tcp or udp (or icmp)")

        if key == "proxy":
            if len(values) != 5:
                logger.warn("Expected 5 values in type %s, received %i" % (key, len(values)))
            self.add_metadata("credential", values[:2])
            if len(values[2:]) == 1:
                self.add_metadata("proxy_address", values[2])
            else:
                self.add_metadata("proxy_socketaddress", values[2:])

        if key == "ftp":
            if len(values) != 3:
                logger.warn("Expected 3 values in type %s, received %i" % (key, len(values)))
            self.add_metadata("credential", values[:2])
            if len(values) >= 3:
                self.add_metadata("url", values[2])

        if key == "rsa_public_key":
            if len(values) != 2:
                logger.warn("Expected 3 values in type %s, received %i" % (key, len(values)))

        if key == "rsa_private_key":
            if len(values) != 8:
                logger.warn("Expected 8 values in type %s, received %i" % (key, len(values)))

    def _add_metadata_dictofstrings(self, key, value):
        # check for type of other?
        for subkey, subvalue in value.items():
            if isinstance(subvalue, (bytes, str)):
                subkey = convert_to_unicode(subkey)
                subvalue = convert_to_unicode(subvalue)
                obj = self.metadata.setdefault(key, {})
                if subkey in obj:
                    # this key already exists, we don't want to clobber so
                    # we turn into list?
                    existing_value = obj[subkey]
                    if isinstance(existing_value, list):
                        if subvalue not in obj[subkey]:
                            obj[subkey].append(subvalue)
                    elif subvalue != existing_value:
                        obj[subkey] = [existing_value, subvalue]
                else:
                    # normal insert of single value
                    obj[subkey] = subvalue
            else:
                # TODO: support inserts of lists (assuming members are strings)?
                logger.warn("Could not add object of %s to metadata under other using key %s" % (
                    str(type(subvalue[subkey])), subkey))

    def add_metadata(self, key, value):
        """
        Report a metadata item

        Primary method to report metadata as a result of parsing.

        Args:
            key: string specifying the key of the metadata. Should be one of values specified in fields.json.
            value: string specifying the value of the metadata. Should be a utf-8 encoded string or a unicode object.

        """
        keyu = convert_to_unicode(key)
        if value is None or all(not _value for _value in value):
            logger.warn("no values provided for %s, skipping" % key)
            return

        if keyu not in self.fields:
            raise KeyError('Invalid field name: {}'.format(keyu))

        fieldtype = self.fields[keyu]['type']

        try:
            if fieldtype == "listofstrings":
                self._add_metatadata_listofstrings(keyu, value)

            if fieldtype == "listofstringtuples":
                self._add_metadata_listofstringtuples(keyu, value)

            if fieldtype == "dictofstrings":
                self._add_metadata_dictofstrings(keyu, value)
        except Exception:
            logger.exception("Error adding metadata for key: {}".format(keyu))

    def run_parser(self, name, file_path=None, data=b"", **kwargs):
        """
        Runs specified parser on file

        :param str name: name of parser module to run (use ":" notation to specify source if necessary e.g. "mwcp-acme:Foo")
        :param str file_path: file to parse
        :param bytes data: use data as file instead of loading data from filename
        """
        self.__reset()

        # TODO: Remove all traces of the input file in the reporter!!
        #  (kept around for now because tool.py uses it for pulling file info)
        if file_path:
            with open(file_path, 'rb') as f:
                self.input_file = mwcp.FileObject(
                    f.read(), self, file_name=os.path.basename(file_path), output_file=False)
                self.input_file.file_path = file_path
        else:
            self.input_file = mwcp.FileObject(data, self, output_file=False)

        try:
            with self.__redirect_stdout():
                found = False
                for source, parser in mwcp.iter_parsers(name):
                    found = True
                    try:
                        parser.parse(self.input_file, self)
                    except (Exception, SystemExit):
                        logger.exception("Error running parser {}:{} on {}".format(
                            source.name, parser.name, file_path or self.input_file.md5))

                if not found:
                    logger.error('Could not find parsers with name: {}'.format(name))
        finally:
            self.__cleanup()

    def output_file(self, data, filename, description=''):
        """
        Report a file created by the parser

        This should involve a file created by the parser and related to the malware.

        :param bytes data: The contents of the output file
        :param str filename: filename (basename) of file
        :param str description: description of the file
        """
        md5 = hashlib.md5(data).hexdigest()

        # TODO: Add filename sanitization.

        # Rename file if we have a name collision.
        num_char = 5
        orig_filename = filename
        while filename in self.outputfiles:
            if md5 == self.outputfiles[filename]['md5']:
                logger.info('Ignoring duplicate output file: {}'.format(filename))
                return
            assert num_char <= 32  # We shouldn't get into an infinite loop due to the check above.
            filename = orig_filename + '_' + md5[:num_char]
            num_char += 1
        if orig_filename != filename:
            logger.info('Renamed {} to {}'.format(orig_filename, filename))

        basename = os.path.basename(filename)
        self.outputfiles[filename] = {
            'data': data, 'description': description, 'md5': md5}

        if self._base64_output_files:
            self.add_metadata(
                "outputfile", [basename, description, md5, base64.b64encode(data)])
        else:
            self.add_metadata("outputfile", [basename, description, md5])

        if self._disable_output_files:
            return

        if self._output_file_prefix:
            if self._output_file_prefix == "md5":
                fullpath = os.path.join(self._output_dir, "%s_%s" % (
                    self.input_file.md5, basename))
            else:
                fullpath = os.path.join(self._output_dir, "%s_%s" % (
                    self._output_file_prefix, basename))
        else:
            fullpath = os.path.join(self._output_dir, basename)

        try:
            with open(fullpath, "wb") as f:
                f.write(data)
            logger.info("Output file: %s" % (fullpath))
            self.outputfiles[filename]['path'] = fullpath
        except Exception as e:
            logger.error("Failed to write output file: %s, %s" % (fullpath, str(e)))

    # TODO: Deprecate this function, we should be interfacing with FileObject instead.
    def report_tempfile(self, filename, description=''):
        """
        load filename from filesystem and report using output_file
        """
        if os.path.isfile(filename):
            with open(filename, "rb") as f:
                data = f.read()
            self.output_file(data, os.path.basename(filename), description)
        else:
            logger.info(
                "Could not output file because it could not be found: %s" % filename)

    def format_list(self, values, key=None):

        if key == "credential" and len(values) == 2:
            return "%s:%s" % (values[0], values[1])
        elif key == "outputfile" and len(values) >= 3:
            return "%s %s" % (values[0], values[1])
        elif key in ("port", "listenport") and len(values) == 2:
            return "%s/%s" % (values[0], values[1])
        elif key == "registrykeyvalue" and len(values) == 2:
            return "%s=%s" % (values[0], values[1])
        elif key in ("socketaddress", "c2_socketaddress", "proxy_socketaddress") and len(values) == 3:
            return "%s:%s/%s" % (values[0], values[1], values[2])
        elif key == "service" and len(values) == 5:
            return ", ".join(values)
        else:
            return ' '.join(values)

    def print_keyvalue(self, key, value):
        print(
            self.get_printable_key_value(key, value),
            file=ascii_writer(getattr(sys.stdout, 'buffer', sys.stdout), 'backslashreplace')
        )

    def print_report(self):
        """
        Output in human readable report format
        """
        # Use sys.stdout.buffer if it exists, which is the case for Python 3 and is required
        # for writing a bytes string. Otherwise just write to whatever is at sys.stdout
        print(
            self.get_output_text(),
            file=ascii_writer(getattr(sys.stdout, 'buffer', sys.stdout), 'backslashreplace')
        )

    def get_printable_key_value(self, key, value):
        output = u""
        printkey = key

        if isinstance(value, (str, bytes)):
            output += u"{:20} {}\n".format(printkey, convert_to_unicode(value))
        else:
            for item in value:
                if isinstance(item, (str, bytes)):
                    output += u"{:20} {}\n".format(printkey, convert_to_unicode(item))
                else:
                    output += u"{:20} {}\n".format(printkey, self.format_list(item, key=key))
                printkey = u""

        return output

    def get_output_text(self):
        """
        Get data in human readable report format.
        """

        output = u""
        infoorderlist = INFO_FIELD_ORDER
        fieldorderlist = STANDARD_FIELD_ORDER

        if 'inputfilename' in self.metadata:
            output += u"\n----File Information----\n\n"
            for key in infoorderlist:
                if key in self.metadata:
                    output += self.get_printable_key_value(
                        key, self.metadata[key])

        output += u"\n----Standard Metadata----\n\n"

        for key in fieldorderlist:
            if key in self.metadata:
                output += self.get_printable_key_value(key, self.metadata[key])

        # in case we have additional fields in fields.json but the order is not
        # updated
        for key in self.metadata:
            if key not in fieldorderlist and key not in ["other", "debug", "outputfile"] and key in self.fields:
                output += self.get_printable_key_value(key, self.metadata[key])

        if "other" in self.metadata:
            output += u"\n----Other Metadata----\n\n"
            for key in sorted(list(self.metadata["other"])):
                output += self.get_printable_key_value(
                    key, self.metadata["other"][key])

        # TODO: Should we still be showing these debug logs?
        if "debug" in self.metadata:
            output += u"\n----Debug----\n\n"
            for item in self.metadata["debug"]:
                output += u"{}\n".format(item)

        if "outputfile" in self.metadata:
            output += u"\n----Output Files----\n\n"
            for value in self.metadata["outputfile"]:
                output += self.get_printable_key_value(
                    value[0], (value[1], value[2]))

        if self.errors:
            output += u"\n----Errors----\n\n"
            for item in self.errors:
                output += u"{}\n".format(item)

        return output

    # TODO: remove stdout redirection
    @contextlib.contextmanager
    def __redirect_stdout(self):
        """Redirects stdout temporarily to the logger."""

        class _LogWriter(object):
            def write(self, message):
                logger.info(message)

            def flush(self):
                pass

        orig_stdout = sys.stdout
        sys.stdout = _LogWriter()
        try:
            yield
        finally:
            sys.stdout = orig_stdout

    def __reset(self):
        """
        Reset all the data in the reporter object that is set during the run_parser function

        Goal is to make the reporter safe to use for multiple run_parser instances
        """
        self._managed_tempdir = None
        self.input_file = None

        self.metadata = {}
        self.outputfiles = {}
        self.errors = []

        # To keep backwards compatibility, setup log handler to add errors and debug messages to reporter.
        # TODO: Remove this when the Reporter object should no longer be responsible for logging.
        log_handler = ReporterLogHandler(self)
        logging.root.addHandler(log_handler)
        # Setup a simple format that doesn't contain any runtime variables.
        log_handler.addFilter(logutil.LevelCharFilter())
        log_handler.setFormatter(logging.Formatter("[%(level_char)s] %(message)s"))
        self._log_handler = log_handler

    def __cleanup(self):
        """
        Cleanup things
        """
        # Remove log handler.
        if self._log_handler:
            logging.root.removeHandler(self._log_handler)
            self._log_handler = None

        # Delete temporary directory.
        if not self._disable_temp_cleanup:
            if self._managed_tempdir:
                try:
                    shutil.rmtree(self._managed_tempdir, ignore_errors=True)
                except Exception as e:
                    logger.error("Failed to purge temp dir: %s, %s" %
                                 (self._managed_tempdir, str(e)))
                self._managed_tempdir = ''
        self._managed_tempdir = None

    def __del__(self):
        self.__cleanup()
