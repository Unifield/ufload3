# Routines related to ownCloud

import easywebdav
import datetime, time
import zipfile
import collections
import logging, re

import ufload

def _splitCloudName(x):
    spl = x.split(":", 1)
    # no :, so use the default cloud hostname
    if len(spl) == 1:
        return ('cloud.msf.org', x)
    return (spl[0], spl[1])

def _get_all_files_and_timestamp(dav, d):
    try:
        all_zip = dav.ls(d)
    except easywebdav.client.OperationFailed as e:
        logging.warn(str(e))
        all_zip = []

    ret = []
    for f in all_zip:
        if not f.name or f.name[-1] == '/':
            continue

        fn = f.name.split("/")[-1]

        # We try to extract a timestamp to get an idea of the creation date
        #  Format: Mon, 14 Mar 2016 03:31:40 GMT
        t = time.strptime(f.mtime, '%a, %d %b %Y %H:%M:%S %Z')

        # We don't take into consideration backups that are too recent.
        # Otherwise they could be half uploaded (=> corrupted)
        if abs(time.time() - time.mktime(t)) < 900:
            # print "SKIP", f.name, "(too recent)"
            continue

        if f.name.split(".")[-1] != "zip":
            logging.warn("Ignoring non-zipfile: %s" % f.name)
            continue
        ret.append((t, f.name))
    return ret

def _lookInsideZip(f, dav):
    # Fetch the ToC of the zip file
    z = zipfile.ZipFile(ufload.httpfile.HttpFile(dav.baseurl+f.name,
                                                 dav.session.auth[0],
                                                 dav.session.auth[1]))
    names = z.namelist()
    if len(names) != 1:
        logging.warn("Zipfile %s has unexpected files in it: %s" % (fn, names))
    if len(names) > 0:
        ret.append(names[0])
    z.close()

# returns True if x is matched by the pattern in instance
def _match_instance_name(instance, x):
    ire = '^' + '.*'.join(map(lambda y : re.escape(y), instance.split('%'))) + '$'
    return bool(re.match(ire, x))

# returns True is any of the instances match x
# (returns True for all if instances is empty)
def _match_any_wildcard(instances, x):
    if not instances:
        return True

    for i in instances:
        if _match_instance_name(i, x):
            return True
    return False

def _group_files_to_download(files):
    files.sort()
    files.reverse()
    ret = collections.defaultdict(lambda : [])

    for a in files:
        t, f = a
        if '/' not in f:
            raise Exception("no slash in %s" % f)

        isplit = f.rindex('/')
        filename = f[isplit+1:]
        if '-' not in filename:
            print "unexpected filename:", filename
            continue

        instance = '-'.join(filename.split('-')[:-1])
        ret[instance].append((f, filename))

    return ret

# list_files returns a dictionary of instances
# and for each instance, a list of (path,file) tuples
# in order from new to old.
def list_files(**kwargs):
    host, directory = _splitCloudName(kwargs['where'])
    webdav = easywebdav.connect(host,
                            username=kwargs['user'],
                            password=kwargs['pw'],
                            protocol='https')
    all = _get_all_files_and_timestamp(webdav, "/remote.php/webdav/"+directory)
    all = _group_files_to_download(all)

    inst = []
    if kwargs['instances'] is not None:
        inst = kwargs['instances']

    ret = {}
    for i in all:
        if _match_any_wildcard(inst, i):
            ret[i] = all[i]
    return ret