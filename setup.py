#!/usr/bin/env python
"""
DC3-MWCP: A framework malware configuration parsers. The main focus is standardizing malware parsers and their output.
"""
import os

from setuptools import setup, find_packages


def read(fname):
    with open(os.path.join(os.path.dirname(__file__), fname), 'r') as fo:
        return fo.read()


setup(
    name="mwcp",
    version="1.0.0",
    author="DC3",
    author_email="dcci@dc3.mil",
    description=__doc__,
    long_description=read("README.md"),
    keywords="malware",
    url="http://github.com/Defense-Cyber-Crime-Center/DC3-MWCP/",
    packages=find_packages(),
    include_package_data=True,
    license='MIT',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: MIT License",'
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3'
    ],
    entry_points={
        'console_scripts': [
            'mwcp-tool = mwcp.tools.tool:main',
            'mwcp-client = mwcp.tools.client:main',
            'mwcp-server = mwcp.tools.server:main',
            'mwcp-test = mwcp.tools.test:main'
        ]
    },
    install_requires=[
        'bottle',
        'future',
        'pefile',
        'requests',
        'six',
    ]
)