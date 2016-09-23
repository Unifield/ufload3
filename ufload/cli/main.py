import ConfigParser, argparse, os, sys
import subprocess

import ufload

def pg_pass(args):
    env = os.environ.copy()
    if args.db_pw is not None:
        env['PGPASSWORD'] = args.db_pw
    return env

# Find exe by looking in the PATH, prefering the one
# installed by the AIO.
def find_exe(exe):
    if sys.platform == "win32":
        path = [ 'D:\\MSF Data\\Unifield\\PostgreSQL\\bin',
                 os.environ['PATH'].split(';') ]
        bin = exe+".exe"
    else:
        path = os.environ['PATH'].split(':')
        bin = exe

    for p in path:
        fn = os.path.join(p, bin)
        if os.path.exists(fn):
            return fn
    # return the unqualified binary name and hope for
    # the best...
    return bin

def pg_common(args):
    res = []
    if args.db_host is not None:
        res.append('-h')
        res.append(args.db_host)
    if args.db_port is not None:
        res.append('-p')
        res.append(args.db_port)
    if args.db_user is not None:
        res.append('-U')
        res.append(args.db_user)
    return res

def pg_restore(args):
    return [ find_exe('pg_restore') ] + pg_common(args)

def psql(args):
    return [ find_exe('psql') ] + pg_common(args)

# Returns a list with the arguments for pg_restore
def _restore_file(args, db, file):
    res = pg_restore(args)
    res.append('--no-acl')
    res.append('--no-owner')
    res.append('-d')
    res.append(db)
    res.append(file)
    return res

def _drop_db(args, db):
    cmd = psql(args)
    cmd.append('-q')
    cmd.append('-c')
    cmd.append('DROP DATABASE IF EXISTS \"%s\"' % db)
    cmd.append('postgres')
    return cmd

def _create_db(args, db):
    cmd = psql(args)
    cmd.append('-q')
    cmd.append('-c')
    cmd.append('CREATE DATABASE \"%s\"' % db)
    cmd.append('postgres')
    return cmd

def _progress(p):
    print >> sys.stderr, p
ufload.progress = _progress

def _ocToDir(oc):
    x = oc.lower()
    if x == 'oca':
        return 'OCA_Backups'
    elif x == 'ocb':
        return 'OCB_Backups'
    elif x == 'ocg':
        return 'UNIFIELD-BACKUP'
    else:
        # no OC abbrev, assume this is a real directory name
        return oc

def _required(args, req):
    err = 0
    for r in req:
        if getattr(args, r) is None:
            print 'Argument %s is required for this sub-command.' % r
            err += 1
    return err == 0

def _run(args, cmd):
    if args.show:
        print "Would run:", " ".join(cmd)
        rc = 0
    else:
        rc = subprocess.call(cmd, env=pg_pass(args))
        if rc != 0:
            print "pg_restore error code: %d" % rc
    return rc

# Turn
# ../databases/OCG_MM1_WA-20160831-220427-A-UF2.1-2p3.dump into OCG_MM1_WA
def _find_instance(fn):
    fn = os.path.basename(fn)
    if '-' not in fn:
        return None
    return fn.split('-')[0]

def _cmdRestore(args):
    if args.file is None:
        if not _required(args, [ 'user', 'pw', 'oc' ]):
            print 'Without the -file argument, ownCloud login info is needed.'
            return 2

    if args.file is not None:
        if args.i is not None:
            if len(args.i) != 1:
                print "Expected only one -i argument."
                return 3
            db = args.i[0]
        else:
            db = _find_instance(args.file)
            if db is None:
                print "Could not guess instance from filename. Use -i to specify it."
                return 3

        rc = _run(args, _drop_db(args, db))
        if rc != 0:
            return rc
        rc = _run(args, _create_db(args, db))
        if rc != 0:
            return rc
        cmd = _restore_file(args, db, args.file)
        return _run(args, cmd)

    # if we got here, we are in fact doing a multi-restore
    print "multi-restore not impl"
    return 1

def _cmdLs(args):
    if not _required(args, [ 'user', 'pw', 'oc' ]):
        return 2

    files = ufload.cloud.list_files(user=args.user,
                                    pw=args.pw,
                                    where=_ocToDir(args.oc),
                                    instances=args.i)
    if len(files) == 0:
        print "No files found."
        return 1

    for i in files:
        for j in files[i]:
            print j[1]

    return 0

def main():
    parser = argparse.ArgumentParser(prog='ufload')

    parser.add_argument("-user", help="ownCloud username")
    parser.add_argument("-pw", help="ownCloud password")
    parser.add_argument("-oc", help="ownCloud directory (OCG, OCA, OCB accepted as shortcuts)")

    parser.add_argument("-db-host", help="Postgres host")
    parser.add_argument("-db-port", help="Postgres port")
    parser.add_argument("-db-user", help="Postgres user")
    parser.add_argument("-db-pw", help="Postgres password")

    sub = parser.add_subparsers(title='subcommands',
                                description='valid subcommands',
                                help='additional help')

    pLs = sub.add_parser('ls', help="List available backups")
    pLs.add_argument("-i", action="append", help="instances to work on (use % as a wildcard)")
    pLs.set_defaults(func=_cmdLs)

    pRestore = sub.add_parser('restore', help="Restore a database from ownCloud or a file")
    pRestore.add_argument("-i", action="append", help="instances to work on (use % as a wildcard)")
    pRestore.add_argument("-n", dest='show', action='store_true', help="no real work; only show what would happen")
    pRestore.add_argument("-file", help="the file to restore (disabled ownCloud downloading)")
    pRestore.set_defaults(func=_cmdRestore)

    # read from $HOME/.ufload first
    conffile = ConfigParser.SafeConfigParser()
    conffile.read('%s/.ufload' % home())
    for subp, subn in ((parser, "owncloud"),
                       (parser, "postgres"),
                       (pLs, "ls"),
                       (pRestore, "restore")):
        if conffile.has_section(subn):
            subp.set_defaults(**dict(conffile.items(subn)))

    # now that the config file is applied, parse from cmdline
    args = parser.parse_args()
    if hasattr(args, "func"):
        sys.exit(args.func(args))

def home():
    if sys.platform == "win32" and 'USERPROFILE' in os.environ:
        return os.environ['USERPROFILE']
    return os.environ['HOME']