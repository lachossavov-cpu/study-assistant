"""Standard-library modules a packaged shell must carry, whether or not anything imports
them today.

PyInstaller only bundles what it can see being imported. The program is no longer
compiled into the shell — it is loaded from files so it can be updated — which means the
packager cannot see its imports at all. Worse, an UPDATE may start using a module the
shell was never built with, and that would crash on a tester's machine with no way for
them to fix it. So the shell carries a broad slice of the standard library up front.

Adding a third-party library is different: that needs a new shell, and the release must
raise `min_shell` so older shells ask for a fresh download instead of crashing.
"""
import argparse, ast, asyncio, base64, bisect, calendar, codecs, collections  # noqa: E401,F401
import colorsys, concurrent.futures, configparser, contextlib, copy, csv, ctypes  # noqa: E401,F401
import dataclasses, datetime, decimal, difflib, email, email.mime.text, enum  # noqa: E401,F401
import fnmatch, fractions, functools, getpass, glob, gzip, hashlib, heapq, hmac  # noqa: E401,F401
import html, html.parser, http.client, http.cookies, http.server, imaplib  # noqa: E401,F401
import importlib, importlib.util, inspect, io, ipaddress, itertools, json, locale  # noqa: E401,F401
import logging, logging.handlers, lzma, math, mimetypes, numbers, operator, os  # noqa: E401,F401
import pathlib, pickle, platform, plistlib, pprint, queue, random, re, runpy  # noqa: E401,F401
import secrets, select, shlex, shutil, signal, smtplib, socket, sqlite3, ssl  # noqa: E401,F401
import statistics, string, struct, subprocess, sys, tarfile, tempfile, textwrap  # noqa: E401,F401
import threading, time, timeit, tomllib, traceback, types, typing, unicodedata  # noqa: E401,F401
import urllib.error, urllib.parse, urllib.request, uuid, warnings, wave, weakref  # noqa: E401,F401
import webbrowser, xml.etree.ElementTree, zipfile, zlib, zoneinfo  # noqa: E401,F401
