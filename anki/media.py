# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU GPL, version 3 or later; http://www.gnu.org/copyleft/gpl.html

"""\
Media support
====================
"""
__docformat__ = 'restructuredtext'

import os, shutil, re, urllib2, time
from anki.db import *
from anki.utils import checksum, genID
from anki.lang import _

regexps = ("(?i)(\[sound:([^]]+)\])",
           "(?i)(<img[^>]+src=[\"']?([^\"'>]+)[\"']?[^>]?>)")

# Tables
##########################################################################

mediaTable = Table(
    'media', metadata,
    Column('id', Integer, primary_key=True, nullable=False),
    Column('filename', UnicodeText, nullable=False),
    # reused as reference count
    Column('size', Integer, nullable=False),
    # treated as modification date, not creation date
    Column('created', Float, nullable=False),
    # reused as md5sum. empty string if file doesn't exist on disk
    Column('originalPath', UnicodeText, nullable=False, default=u""),
    # older versions stored original filename here, so we'll leave it for now
    # in case we add a feature to rename media back to its original name. in
    # the future we may want to zero this to save space
    Column('description', UnicodeText, nullable=False, default=u""))

class Media(object):
    pass

mapper(Media, mediaTable)

mediaDeletedTable = Table(
    'mediaDeleted', metadata,
    Column('mediaId', Integer, ForeignKey("cards.id"),
           nullable=False),
    Column('deletedTime', Float, nullable=False))

# File handling
##########################################################################

def copyToMedia(deck, path):
    """Copy PATH to MEDIADIR, and return new filename.

If a file with the same md5sum exists in the DB, return that.
If a file with the same name exists, return a unique name.
This does not modify the media table."""
    # see if have duplicate contents
    newpath = deck.s.scalar(
        "select filename from media where originalPath = :cs",
        cs=checksum(open(path, "rb").read()))
    # check if this filename already exists
    if not newpath:
        base = os.path.basename(path)
        mdir = deck.mediaDir(create=True)
        newpath = uniquePath(mdir, base)
        shutil.copy2(path, newpath)
    return os.path.basename(newpath)

def uniquePath(dir, base):
    # remove any dangerous characters
    base = re.sub(r"[][<>:/\\]", "", base)
    # find a unique name
    (root, ext) = os.path.splitext(base)
    def repl(match):
        n = int(match.group(1))
        return " (%d)" % (n+1)
    while True:
        path = os.path.join(dir, root + ext)
        if not os.path.exists(path):
            break
        reg = " \((\d+)\)$"
        if not re.search(reg, root):
            root = root + " (1)"
        else:
            root = re.sub(reg, repl, root)
    return path

# DB routines
##########################################################################

def updateMediaCount(deck, file, count=1):
    mdir = deck.mediaDir()
    if deck.s.scalar(
        "select 1 from media where filename = :file", file=file):
        deck.s.statement(
            "update media set size = size + :c, created = :t where filename = :file",
            file=file, c=count, t=time.time())
    elif count > 0:
        try:
            sum = unicode(
                checksum(open(os.path.join(mdir, file), "rb").read()))
        except:
            sum = u""
        deck.s.statement("""
insert into media (id, filename, size, created, originalPath, description)
values (:id, :file, :c, :mod, :sum, '')""",
                         id=genID(), file=file, c=count, mod=time.time(),
                         sum=sum)

def removeUnusedMedia(deck):
    ids = deck.s.column0("select id from media where size = 0")
    for id in ids:
        deck.s.statement("insert into mediaDeleted values (:id, :t)",
                         id=id, t=time.time())
    deck.s.statement("delete from media where size = 0")

# String manipulation
##########################################################################

def mediaFiles(string):
    l = []
    for reg in regexps:
        for (full, fname) in re.findall(reg, string):
            l.append(fname)
    return l

def stripMedia(txt):
    for reg in regexps:
        txt = re.sub(reg, "", txt)
    return txt

# Rebuilding DB
##########################################################################

def rebuildMediaDir(deck, delete=False, dirty=True):
    deck.startProgress(title=_("Check Media DB"))
    mdir = deck.mediaDir(create=True)
    # set all ref counts to 0
    deck.s.statement("update media set size = 0")
    # look through cards for media references
    refs = {}
    for (question, answer) in deck.s.all(
        "select question, answer from cards"):
        for txt in (question, answer):
            for f in mediaFiles(txt):
                if f in refs:
                    refs[f] += 1
                else:
                    refs[f] = 1
    # update ref counts
    for (file, count) in refs.items():
        updateMediaCount(deck, file, count)
    # find unused media
    unused = []
    for file in os.listdir(mdir):
        path = os.path.join(mdir, file)
        if not os.path.isfile(path):
            # ignore directories
            continue
        if file not in refs:
            unused.append(file)
    # optionally delete
    if delete:
        removeUnusedMedia(deck)
        for f in unused:
            path = os.path.join(mdir, f)
            os.unlink(path)
    # check md5s are up to date
    update = []
    for (file, created, md5) in deck.s.all(
        "select filename, created, originalPath from media"):
        path = os.path.join(mdir, file)
        if not os.path.exists(path):
            if md5:
                update.append({'f':file, 'sum':u"", 'c':time.time()})
        else:
            sum = unicode(
                checksum(open(os.path.join(mdir, file), "rb").read()))
            if md5 != sum:
                update.append({'f':file, 'sum':sum, 'c':time.time()})
    if update:
        deck.s.statements("""
update media set originalPath = :sum, created = :c where filename = :f""",
                          update)
    # update deck and get return info
    if dirty:
        deck.flushMod()
    nohave = deck.s.column0("select filename from media where originalPath = ''")
    deck.finishProgress()
    return (nohave, unused)

# Download missing
##########################################################################

def downloadMissing(deck):
    urlbase = deck.getVar("mediaURL")
    if not urlbase:
        return None
    mdir = deck.mediaDir(create=True)
    deck.startProgress()
    missing = 0
    grabbed = 0
    for c, (f, sum) in enumerate(deck.s.all(
        "select filename, originalPath from media")):
        path = os.path.join(mdir, f)
        if not os.path.exists(path):
            try:
                rpath = urlbase + f
                url = urllib2.urlopen(rpath)
                open(f, "wb").write(url.read())
                grabbed += 1
            except:
                if sum:
                    # the file is supposed to exist
                    deck.finishProgress()
                    return (False, rpath)
                else:
                    # ignore and keep going
                    missing += 1
        deck.updateProgress(label=_("File %d...") % (grabbed+missing))
    deck.finishProgress()
    return (True, grabbed, missing)
