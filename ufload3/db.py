import os, sys, subprocess, tempfile, hashlib, urllib.request, urllib.parse, urllib.error, zipfile, base64
import xmlrpc.client
import ufload3
import re

def _run_out(args, cmd):
    try:
        return str(subprocess.check_output(cmd, env=pg_pass(args), stderr=subprocess.STDOUT), 'utf8').split('\n')
    except Exception as e:
        ufload3.progress("Unexpected error %s" % e)
        return []

def _run(args, cmd, get_out=False, silent=False):
    if args.show:
        ufload3.progress("Would run: " + str(cmd))
        rc = 0
    else:
        if silent or get_out:
            out = ""
            try:
                out = subprocess.check_output(cmd, env=pg_pass(args), stderr=subprocess.STDOUT)
                return 0, str(out, 'utf8')
            except subprocess.CalledProcessError as exc:
                return exc.returncode, exc.output
        else:
            rc = subprocess.call(cmd, env=pg_pass(args))
    return rc

# Find exe by looking in the PATH, prefering the one
# installed by the AIO (UF6.0 style or pre-UF6 style)
def _find_exe(exe):
    if sys.platform == "win32":
        path = [ r'c:\Program Files (x86)\msf\Unifield\pgsql\bin',
                 r'd:\MSF Data\Unifield\PostgreSQL\bin' ]
        path.extend(os.environ['PATH'].split(';'))
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
    cmd = [ _find_exe('pg_restore') ] + pg_common(args)
    if args.jobs:
        cmd += ['-j', '%s'%args.jobs]
    return cmd

def pg_pass(args):
    env = os.environ.copy()
    if args.db_pw is not None:
        env['PGPASSWORD'] = args.db_pw
    return env

def mkpsql(args, sql, db='postgres'):
    cmd = [ _find_exe('psql') ] + pg_common(args)
    cmd.append('-q')
    cmd.append('-t')
    cmd.append('-c')
    cmd.append(sql)
    cmd.append(db)
    return cmd

def mkpsql_file(args, file, db='postgres'):
    cmd = [ _find_exe('psql') ] + pg_common(args)
    cmd.append('-q')
    cmd.append('-t')
    cmd.append('-f')
    cmd.append(file)
    cmd.append(db)
    return cmd

def psql(args, sql, db='postgres', silent=False):
    return _run(args, mkpsql(args, sql, db), silent)

def psql_file(args, file, db='postgres', silent=False):
    return _run(args, mkpsql_file(args, file, db), silent)
    
def load_zip_into(args, db, f, sz):
    if sz == 0:
        ufload3.progress("Note: No progress percent available.")
    
    db2 = db + "_" + str(os.getpid())

    ufload3.progress("Create database "+db2)
    tablespace = ""
    if args.db_tablespace:
        tablespace = 'TABLESPACE "%s"'%args.db_tablespace
    rc = psql(args, 'CREATE DATABASE \"%s\" %s' % (db2, tablespace))
    if rc != 0:
        return rc

    # From here out, we need a try block, so that we can drop
    # the temp db if anything went wrong
    try:
        ufload3.progress("Restoring into %s" % db2)
    
        cmd = pg_restore(args)
        cmd.append('--no-acl')
        cmd.append('--no-owner')
        cmd.append('-d')
        cmd.append(db2)
        cmd.append('-n')
        cmd.append('public')
        cmd.append('-S')
        cmd.append(args.db_user)
        cmd.append('--disable-triggers')

        if not args.show:
            with open(f, 'rb') as fileobj:
                z = zipfile.ZipFile(fileobj)
                names = z.namelist()
                fn = names[0]
                #z.extract(fn)
                z.extractall()
                z.close()
                del z
            os.unlink(f)

            cmd.append(fn)

            ufload3.progress("Starting restore. This will take some time.")
            try:
                rc =_run(args, cmd)
            except KeyboardInterrupt:
                raise dbException(1)

            # clean up the temp file
            try:
                os.unlink(fn)
            except OSError:
                pass

        else:
            ufload3.progress("Would run: "+ str(cmd))
            rc = 0

        rcstr = "ok"
        if rc != 0:
            rcstr = "error %d" % rc
        ufload3.progress("Restore finished with result code: %s" % rcstr)
        _checkrc(rc)

        # Let's delete uninstalled versions
        rc = psql(args, 'DELETE FROM sync_client_version WHERE state!=\'installed\'', db2)
        _checkrc(rc)
        psql(args, 'update sync_client_version set patch=NULL', db2)
        psql(args, 'vacuum full sync_client_version', db2)
        psql(args, "update sync_client_survey set active ='f'", db2)
        if 'SYNC' in db2:
            psql(args, "update sync_server_survey set active ='f'", db2)
            psql(args, 'alter table sync_server_entity ADD COLUMN IF NOT EXISTS ufload_hardware_id_prod_value varchar(128);', db2)
            psql(args, 'update sync_server_entity set ufload_hardware_id_prod_value=hardware_id;', db2)

        # Analyze DB to optimize queries (rebuild indexes...)
        if args.analyze:
            ufload3.progress("Analyzing database %s and rebuilding indexes" % db2)
            rc = psql(args, 'ANALYZE', db2)
            _checkrc(rc)

        _checkrc(delive(args, db2))
        
        ufload3.progress("Drop database "+db)
        killCons(args, db)
        rc = psql(args, 'DROP DATABASE IF EXISTS \"%s\"'%db)
        # First, revoke CONNECT rights to the DB so there won't be any auto-connect issues
        psql(args, 'GRANT CONNECT ON DATABASE %s FROM public' % db, 'postgres', True)
        _checkrc(rc)

        ufload3.progress("Rename database %s to %s" % (db2, db))
        rc = psql(args, 'ALTER DATABASE \"%s\" RENAME TO \"%s\"' % (db2, db))
        _checkrc(rc)

        # analyze db
        psql(args, 'analyze', db, silent=True)

        instantiate(args, db)

        for d in _allDbs(args):
            if d.startswith(db) and d!=db:
                ufload3.progress("Cleaning other database for instance %s: %s" % (db, d))
                killCons(args, d)
                rc = psql(args, 'DROP DATABASE IF EXISTS \"%s\"' % d)
                if rc != 0:
                    return rc

        return 0
    except Exception:
        ufload3.progress("Unexpected error %s" % sys.exc_info()[0])
        # something went wrong, so drop the temp table
        ufload3.progress("Cleanup: dropping table %s" % db2)
        killCons(args, db2)
        psql(args, 'DROP DATABASE \"%s\"'%db2)
        return 1


def load_dump_into(args, db, f, sz):
    tot = float(sz)
    if sz == 0:
        ufload3.progress("Note: No progress percent available.")

    db2 = db + "_" + str(os.getpid())

    ufload3.progress("Create database " + db2)
    tablespace = ""
    if args.db_tablespace:
        tablespace = 'TABLESPACE "%s"'%args.db_tablespace
    rc = psql(args, 'CREATE DATABASE \"%s\" %s' % (db2, tablespace))
    if rc != 0:
        return rc

    # From here out, we need a try block, so that we can drop
    # the temp db if anything went wrong
    try:
        ufload3.progress("Restoring into %s" % db2)

        cmd = pg_restore(args)
        cmd.append('--no-acl')
        cmd.append('--no-owner')
        cmd.append('-d')
        cmd.append(db2)
        cmd.append('-n')
        cmd.append('public')
        cmd.append('-S')
        cmd.append(args.db_user)
        cmd.append('--disable-triggers')

        # Windows pg_restore gets confused when reading from a pipe,
        # so write to a temp file first.
        #if sys.platform == "win32":
        if sys.platform == "win32" or args.jobs :  #  pg_restore from standard input cannot use -j option
            tf = tempfile.NamedTemporaryFile(delete=False)
            if not args.show:

                n = 0
                next = 10
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    tf.write(chunk)
                    n += len(chunk)
                    if tot != 0:
                        pct = n / tot * 100
                        if pct > next:
                            ufload3.progress("Loading data: %d%%" % int(pct))
                            next = int(pct / 10) * 10 + 10

            tf.close()
            cmd.append(tf.name)

            ufload3.progress("Starting restore. This will take some time.")
            try:
                rc = _run(args, cmd)
            except KeyboardInterrupt:
                raise dbException(1)

            if 'SYNC' in db2:
                psql(args, 'alter table sync_server_entity ADD COLUMN IF NOT EXISTS ufload_hardware_id_prod_value varchar(128);', db2)
                psql(args, 'update sync_server_entity set ufload_hardware_id_prod_value=hardware_id;', db2)

            # clean up the temp file
            try:
                os.unlink(tf.name)
            except OSError:
                pass
        else:
            # For non-Windows, feed the data in via pipe so that we have
            # some progress indication.
            if not args.show:
                p = subprocess.Popen(cmd, bufsize=1024 * 1024 * 10,
                                     stdin=subprocess.PIPE,
                                     stdout=sys.stdout,
                                     stderr=sys.stderr,
                                     env=pg_pass(args))

                n = 0
                next = 10
                for chunk in iter(lambda: f.read(8192), b''):
                    try:
                        p.stdin.write(chunk)
                    except IOError:
                        break
                    n += len(chunk)
                    if tot != 0:
                        pct = n / tot * 100
                        if pct > next:
                            ufload3.progress("Restoring: %d%%" % int(pct))
                            next = int(pct / 10) * 10 + 10

                p.stdin.close()
                ufload3.progress("Restoring: 100%")
                ufload3.progress("Waiting for Postgres to finish restore")
                rc = p.wait()
            else:
                ufload3.progress("Would run: " + str(cmd))
                rc = 0

        rcstr = "ok"
        if rc != 0:
            rcstr = "error %d" % rc
        ufload3.progress("Restore finished with result code: %s" % rcstr)
        _checkrc(rc)

        #USELESS FOR SYNC SERVER Let's delete uninstalled versions
        #rc = psql(args, 'DELETE FROM sync_server_version WHERE state!=\'installed\'', db)
        #_checkrc(rc)

        _checkrc(delive(args, db2))

        ufload3.progress("Drop database " + db)
        killCons(args, db)
        rc = psql(args, 'DROP DATABASE IF EXISTS \"%s\"' % db)
        _checkrc(rc)

        ufload3.progress("Rename database %s to %s" % (db2, db))
        rc = psql(args, 'ALTER DATABASE \"%s\" RENAME TO \"%s\"' % (db2, db))
        _checkrc(rc)
        instantiate(args, db)

        return 0
    except dbException as e:
        # something went wrong, so drop the temp table
        ufload3.progress("Unexpected error %s" % sys.exc_info()[0])
        ufload3.progress("Cleanup: dropping db %s" % db2)
        killCons(args, db2)
        psql(args, 'DROP DATABASE \"%s\"' % db2)
        return e.rc
    except:
        ufload3.progress("Unexpected error %s" % sys.exc_info()[0])
        ufload3.progress("Cleanup: dropping db %s" % db2)
        killCons(args, db2)
        psql(args, 'DROP DATABASE \"%s\"' % db2)
        return 1

def instantiate(args, db):
    if args.instantiate:
        port = 8069
        if args.sync_xmlrpcport:
            port = int(args.sync_xmlrpcport)

        try:
            host = '127.0.0.1'
            transport = xmlrpc.client.Transport()
            connection = transport.make_connection(host)
            connection.timeout = 4
            sock = xmlrpc.client.ServerProxy('http://%s:%s/xmlrpc/common' % (host, port), transport=transport)
            uid = sock.login(db, args.adminuser.lower(), args.adminpw)
        except Exception as e:
            ufload3.progress("non blocking error at first connection %s" % e)

# De-live uses psql to change a restored database taken from a live backup
# into a non-production, non-live database. It:
# 1. stomps all existing passwords
# 2. changes the sync connection to a local one
# 3. removes cron jobs for backups and sync and automated imports/exports
# 4. remove the automated imports/exports settings
# 4. set the backup directory
def delive(args, db):

    if args.live:
        ufload3.progress("*** WARNING: The restored database has LIVE passwords and LIVE syncing and LIVE settings for automated imports/exports.")
        if args.sync:
            ufload3.progress("(please note that ufload is not able to connect to the sync server using live passwords, please connect manually)")
        return 0

    adminuser = args.adminuser.lower()
    port = 8069
    if args.sync_xmlrpcport:
        port = int(args.sync_xmlrpcport)

    ss = 'SYNC_SERVER_LOCAL'
    if args.ss:
        ss = args.ss

    rc = psql(args, 'alter table sync_client_sync_server_connection ADD COLUMN IF NOT EXISTS ufload_automatic_patching_prod_value boolean;', db)
    rc = psql(args, 'update sync_client_sync_server_connection set ufload_automatic_patching_prod_value=automatic_patching;', db)
    rc = psql(args, 'update sync_client_sync_server_connection set automatic_patching = \'f\', protocol = \'xmlrpc\', login = \'%s\', database = \'%s\', host = \'127.0.0.1\', port = %d;' % (adminuser, ss, port), db)
    if rc != 0:
        return rc

    # disable cron jobs
    rc = psql(args, "update ir_cron set active = 'f' where model in ('backup.config', 'unidata.sync');", db)
    if rc != 0:
        return rc
    rc = psql(args, 'update ir_cron set active = \'f\' where model = \'msf.instance.cloud\';', db)
    if rc != 0:
        return rc
    rc = psql(args, 'update ir_cron set active = \'f\' where model = \'sync.client.entity\';', db)
    if rc != 0:
        return rc
    rc = psql(args, 'update ir_cron set active = \'f\' where model = \'stock.mission.report\';', db)
    if rc != 0:
        return rc

    #Automated import jobs
    rc = psql(args, 'update ir_cron set active = \'f\' where model = \'automated.import\';', db)
    if rc != 0:
        return rc
    # Automated import settings
    psql(args, 'UPDATE automated_import SET report_path=\'\', src_path=\'\', ftp_url=\'\', dest_path=\'\', ftp_ok=\'f\', ftp_port=\'\',dest_path_failure=\'\', ftp_login=\'\', ftp_password=\'\', ftp_protocol=\'\';', db)

    # Automated export jobs
    rc = psql(args, 'update ir_cron set active = \'f\' where model = \'automated.export\';', db)
    if rc != 0:
        return rc
    # Automated export settings
    psql(args, 'UPDATE automated_export SET report_path=\'\', ftp_url=\'\', dest_path=\'\', ftp_ok=\'f\', ftp_port=\'\',dest_path_failure=\'\', ftp_login=\'\', ftp_password=\'\', ftp_protocol=\'\';', db)

    # Now we check for arguments allowing auto-sync and silent-upgrade
    if args.autosync:
        activate_autosync(args, db, ss)
        rc = psql(args, 'update ir_cron set active = \'t\', interval_type = \'hours\', interval_number = 2, nextcall = current_timestamp + interval \'1 hour\' where model = \'sync.client.entity\' and function = \'sync_threaded\';', db)
        if rc != 0:
            return rc
        rc = psql(args, 'update sync_client_sync_server_connection SET host = \'127.0.0.1\', database = \'%s\';' % ss, db)

    if args.silentupgrade:
        if not args.autosync:
            ufload3.progress("*** WARNING: Silent upgrade is enabled, but auto sync is not.")
        rc = psql(args, 'update sync_client_sync_server_connection set automatic_patching = \'t\';', db)
        if rc != 0:
            return rc

    if args.hidegroups:
        psql(args, "truncate ir_ui_view_sc;", db)
        for to_del in args.hidegroups.split(','):
            psql(args, "update res_groups set visible_res_groups='f' where name ilike '%s';" % to_del, db)
            psql(args, "delete from res_groups_users_rel where gid in (select g.id from res_groups g where g.visible_res_groups='f');", db)

    if args.logo:
         psql(args, "update res_company set logo='%s';" % str(base64.b64encode(open(args.logo, 'rb').read()), 'utf8'), db)

    if args.banner:
         psql(args, "update communication_config set message=$ESC$%s$ESC$;" % args.banner, db)

    # Set the backup directory
    directory = 'd:\\'
    if sys.platform != "win32" and args.db_host in [ None, 'ct0', 'localhost' ]:
        # when loading on non-windows, to a local database, use /tmp
        directory = '/tmp'

    if args.backuppath:
        directory = args.backuppath

    rc = psql(args, 'update backup_config set beforemanualsync=\'f\', beforepatching=\'f\', aftermanualsync=\'f\', beforeautomaticsync=\'f\', afterautomaticsync=\'f\', scheduledbackup=\'f\', name = \'%s\';' % directory, db)
    if rc != 0:
        return rc

    # put the chosen password into all users
    if args.userspw:
        rc = psql(args, 'update res_users set password = \'%s\' WHERE id <> 1;' % args.userspw, db)

    if args.pwlist:
        for pwlist in args.pwlist.split(','):
            user, newpw = pwlist.split(':')
            psql(args, 'update res_users set password = \'%s\' WHERE login =\'%s\';' % (newpw, user), db)

    if args.adminpw:
        rc = psql(args, 'update res_users set password = \'%s\' WHERE id = 1;' % args.adminpw, db)

    if args.nopwreset:
        ufload3.progress("*** WARNING: The restored database has LIVE passwords.")
    else:

        # set the username of the admin account
        rc = psql(args, 'update res_users set login = \'%s\' where id = 1;' % adminuser, db)
        if rc != 0:
            return rc

        if args.inactiveusers:
            rc = psql(args, "update res_users set active = 'f' where login not in ('synch', '%s');" % adminuser, db)

    if args.createusers:
        if args.adminpw != args.userspw:
            newpass = args.userspw
        else:
            newpass = args.adminpw

        if args.newuserspw:
            db_name = db
            if args.db_prefix:
                db_name = db_name.split(args.db_prefix+'_', 1)[1]
            new_pass_dict = []
            for pass_part in re.split( '(\[\d+\+\d+\])', args.newuserspw):
                m = re.search('\[(\d+)\+(\d+)\]', pass_part)
                if m:
                    pos = int(m.group(1)) - 1
                    add = int(m.group(2))
                    new_pass_dict.append('%d' % (max(ord(db_name[pos].lower()), 96) - 96 + add, ))
                else:
                    new_pass_dict.append(pass_part)
            if new_pass_dict:
                newpass = ''.join(new_pass_dict)

        for new_user_info in args.createusers.split(';'):
            new_user_data = new_user_info.split(':')
            if len(new_user_data) == 3:
                new_user= new_user_data[0]
                new_user_pass = new_user_data[1]
                groups = new_user_data[2]
            else:
                new_user= new_user_data[0]
                new_user_pass = newpass
                groups = new_user_data[1]
            rc, new_userid = psql(args, """ insert into res_users (name, active, login, password, context_lang, company_id, view, menu_id) values
                ('%s', 't', '%s', '%s', 'en_MF', 1, 'simple', 1) returning id;"""
                % (new_user, new_user.lower(), new_user_pass), db, silent=True)
            if rc != 0:
                return rc
            for new_group in groups.split(','):
                rc = psql(args, " insert into res_groups_users_rel (uid, gid) (select %s, id from res_groups where name='%s');" % (new_userid, new_group), db)
                if rc != 0:
                    return rc
    if args.usersinfo:
        for userinfo in args.usersinfo.split(';'):
            try:
                login, new_user_email, new_user_dpt = userinfo.split(':')
                if new_user_dpt:
                    psql(args, """ update res_users u set context_department_id = d.id
                        from hr_department d
                            where d.name = '%s' and u.login = '%s' """ % (new_user_dpt, login), db)

                if new_user_email:
                    rc, address_id = psql(args, """ insert into res_partner_address (name, email) values ('%s', '%s') returning id """ % (login, new_user_email), db, silent=True)
                    if address_id:
                        psql(args,"update res_users set address_id=%s where login='%s'" % (address_id, login), db)

            except ValueError:
                ufload3.progress("*** WARNING: invalid format %s" % userinfo)

    # ok, delive finished with no problems
    return 0

def activate_autosync(args, db, ss):
    rc = psql(args,
              'update ir_cron set active = \'t\', interval_type = \'hours\', interval_number = 2, nextcall = current_timestamp + interval \'1 hour\' where model = \'sync.client.entity\' and function = \'sync_threaded\';',
              db)
    if rc != 0:
        return rc

    rc = psql(args,
              'update sync_client_sync_server_connection SET host = \'127.0.0.1\', database = \'%s\';' % ss,
              db)

    return rc

def activate_silentupgrade(args, db):
    rc = psql(args, 'update sync_client_sync_server_connection set automatic_patching = \'t\';', db)

    if not args.autosync:
        ufload3.progress("*** WARNING: Silent upgrade is enabled, but auto sync is not.")

    return rc


def _checkrc(rc):
    if rc != 0:
        raise dbException(rc)

class dbException(Exception):
    def __init__(self, rc):
        self.rc = rc

def ver(args):
    v = _run_out(args, mkpsql(args, 'show server_version'))
    return v

def killCons(args, db):
    # A wacky exception for UF5: we are not superuser on Postgres, so we
    # cannot kill connections. So bounce OpenERP instead.
    if args.killconn:
        _run(args, [ 'sh', '-c', args.killconn])
        return

    # First, revoke CONNECT rights to the DB so there won't be any auto-connect issues
    psql(args, 'REVOKE CONNECT ON DATABASE %s FROM public' % db, 'postgres', True)

    col = 'pid'

    cmd = mkpsql(args, 'select %s from pg_stat_activity where datname = \'%s\';' % (col, db), 'postgres')
    for i in _run_out(args, cmd):
        try:
            pid = int(i)
            psql(args, 'select pg_terminate_backend(%s)' % pid, 'postgres', True)
        except ValueError:
            # skip lines which are not numbers
            pass

def get_hwid(args):
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 "SYSTEM\ControlSet001\services\eventlog\Application\openerp-web-6.0",
                                 0, winreg.KEY_READ) as registry_key:
                hwid, regtype = winreg.QueryValueEx(registry_key, "HardwareId")
                ufload3.progress("Hardware id from registry key: %s" % hwid)
                return hwid
        except WindowsError as e:
            ufload3._progress(e)
            return None
    else:
        # Follow the same algorithm that Unifield uses (see sync_client.py)
        mac = []
        for line in os.popen("/sbin/ifconfig"):
            if line.find('Ether') > -1:
                mac.append(line.split()[4])
        mac.sort()
        hw_hash = hashlib.md5((''.join(mac)).encode('utf8')).hexdigest()
        return hw_hash

def _db_to_instance(args, db):
    if args.db_prefix:
        db = db[len(args.db_prefix)+1:]

    ss = 'SYNC_SERVER_LOCAL'
    if args.ss:
        ss = args.ss

    if db.startswith(ss):
        return ss

    return '_'.join(db.split('_')[0:-2])

def cleanDbs(args):

    import re
    p = re.compile('^[A-Z0-9_]{5,}_[0-9]{8}_[0-9]{4}$')
    ps = re.compile('SYNC')

    nb = 0
    for d in _allDbs(args):

        m = p.match(d)
        ms = ps.search(d)

        if m == None and ms == None and d != '':
            ufload3.progress("Dropping database %s" % d)
            killCons(args, d)
            rc = psql(args, 'DROP DATABASE IF EXISTS \"%s\"'%d)
            if rc != 0:
                ufload3.progress("Error: unable to drop database %s" % d)
            else:
                nb = nb + 1

    return nb

def sync_link(args, hwid, db, sdb, all=False):
    instance = _db_to_instance(args, db)
    #Create the instance in the sync server if it does not already exist
    # deactivated: creation does not help, instance must be linked to sync groups and other instances
    #rc = psql(args, 'insert into sync_server_entity (create_uid, create_date, write_date, write_uid, user_id, name, state) SELECT 1, now(), now(), 1, 1, \'%s\', \'validated\' FROM sync_server_entity WHERE NOT EXISTS (SELECT 1 FROM sync_server_entity WHERE name = \'%s\') ' % (instance, instance), sdb )

    #if rc != 0:
    #    ufload3.progress('Unable to create the instance %s on the sync server. Please add it manually.' % instance)
    #    #return rc

    if all:
        # Update hardware id for every instance
        return psql(args, 'update sync_server_entity set hardware_id = \'%s\';' % hwid, sdb)
    else:
        #Update hardware id for this instance
        return psql(args, 'update sync_server_entity set hardware_id = \'%s\' where name = \'%s\';' % (hwid, instance), sdb)

# Remove all databases which come from the same instance as db
def clean(args, db):
    toClean = {}
    toKeep = {}

    i = _db_to_instance(args, db)
    toClean[i] = True
    toKeep[db] = True

    for d in _allDbs(args):
        i = _db_to_instance(args, d)
        #if not args.db_prefix and i and d not in toKeep and i in toClean:
        if i and d not in toKeep and i in toClean:
            ufload3.progress("Cleaning other database for instance %s: %s" % (i, d))
            killCons(args, d)
            rc = psql(args, 'DROP DATABASE IF EXISTS \"%s\"'%d)
            if rc != 0:
                return rc
    return 0            

def _allDbs(args):
    if args.db_user:
        v = _run_out(args, mkpsql(args, 'select datname from pg_database where datdba=(select usesysid from pg_user where usename=\'%s\') and datistemplate = false and datname != \'postgres\'' % args.db_user))
    else:
        v = _run_out(args, mkpsql(args, 'select datname from pg_database where datistemplate = false and datname != \'postgres\''))
        
    return list(filter(len, [x.strip() for x in v]))

def exists(args, db):
    v = _run_out(args, mkpsql(args, 'select datname from pg_database where datname = \'%s\'' % db))
    v = list(filter(len, [x.strip() for x in v]))
    return len(v)==1 and v[0] == db

# These two functions read and write from a little "about" table
# where we store the size of the input file, which helps us avoid
# reloading the sync server when we don't need to.
def get_sync_server_len(args, db='SYNC_SERVER_LOCAL'):
    try:
        #First, check if the db already exists
        exist = _run_out(args, mkpsql(args, 'SELECT 1 FROM information_schema.tables  WHERE table_catalog=\'%s\' AND table_schema=\'public\' AND table_name=\'about\';' % db))
        if len(exist) < 3:
            return -1;

        l = _run_out(args, mkpsql(args, 'select length from about', db))
        if len(l) < 1:
            return -1
        return int(list(filter(len, l))[0])
    except subprocess.CalledProcessError:
        pass
    return -1

def write_sync_server_len(args, l, db='SYNC_SERVER_LOCAL'):
    _run_out(args, mkpsql(args, 'drop table if exists about; create table about ( length int ); insert into about values ( %d )' % l, db))

def sync_server_all_admin(args, db='SYNC_SERVER_LOCAL'):
    _run_out(args, mkpsql(args, 'update sync_server_entity set user_id = 1;', db))

def sync_server_all_sandbox_sync_user(args, db='SYNC_SERVER_LOCAL'):
    _run_out(args, mkpsql(args, "update sync_server_entity set user_id = (select id from res_users where login='%s');" % args.connectionuser, db))
    if args.connectionpw:
        _run_out(args, mkpsql(args, "update res_users set password ='%s' where login='%s';" % (args.connectionpw, args.connectionuser) , db))

def connect_instance_to_sync_server(args, sync_server, db):
    # desactivation because of auto-connect
    return 0

    # if db.startswith('SYNC_SERVER'):
    #    return 0

    port = 8069
    if args.sync_xmlrpcport:
        port = int(args.sync_xmlrpcport)

    try:
        ufload3.progress('Connecting instance %s to %s' % (db, sync_server))

        sock = xmlrpc.client.ServerProxy('http://127.0.0.1:%s/xmlrpc/common' % (port, ))
        uid = sock.login(db, args.adminuser.lower(), args.adminpw)
        sock = xmlrpc.client.ServerProxy('http://127.0.0.1:%s/xmlrpc/object' % (port, ))


        conn_ids = sock.execute(db, uid, args.adminpw, 'sync.client.sync_server_connection', 'search', [])
        if conn_ids:
            current_state = sock.execute(db, uid, args.adminpw, 'sync.client.sync_server_connection', 'read', conn_ids[0], ['state'])
            if current_state['state'] != 'Connected':
                sock.execute(db, uid, args.adminpw, 'sync.client.sync_server_connection', 'write', conn_ids, {'login' : args.connectionuser, 'password': args.connectionpw})
                sock.execute(db, uid, args.adminpw, 'sync.client.sync_server_connection', 'connect', conn_ids)
    except xmlrpc.client.Fault as e:
         ufload3.progress("Error: unable to connect instance to the sync server: %s" % e)
    except:
        ufload3.progress("Unexpected error: unable to connect instance to the sync server: %s" % sys.exc_info()[0])

def manual_sync(args, sync_server, db):
    if db.startswith('SYNC_SERVER'):
        return 0
    ufload3.progress("manual sync instance %s to sync server %s" % (db, sync_server))
    netrpc = connect_rpc(args, db)
    sync_obj = netrpc.get('sync.client.sync_manager')

    sync_ids = sync_obj.search([])
    sync_obj.sync(sync_ids)

def manual_upgrade(args, sync_server, db):
    if db.startswith('SYNC_SERVER'):
        return 0
    ufload3.progress("manual update instance %s to sync server %s" % (db, sync_server))
    netrpc = connect_rpc(args, db)
    sync_obj = netrpc.get('sync_client.upgrade')

    ufload3.progress("Download patch")
    sync_ids = sync_obj.search([])
    result = sync_obj.download(sync_ids)
    if result:
        ufload3.progress("update Unifield")
        result = sync_obj.do_upgrade(sync_ids)
    return result

class connect_rpc:
    def __init__(self, args, db):
        port = 8069
        if args.sync_xmlrpcport:
            port = args.sync_xmlrpcport
        sock = xmlrpc.client.ServerProxy('http://127.0.0.1:%s/xmlrpc/common' % (port, ))
        self.uid = sock.login(db, args.adminuser.lower(), args.adminpw)
        if not self.uid:
            raise Exception('%s, Wrong %s password' % (db, args.adminuser.lower()))
        self.db = db
        self.password = args.adminpw
        self.sock = xmlrpc.client.ServerProxy('http://127.0.0.1:%s/xmlrpc/object' % (port, ))

    def get(self, model):
        return oerp_obj(self.sock, self.db, self.uid, self.password, model)

class oerp_obj:
    def __init__(self, sock, db, uid, password, model):
        self.sock = sock
        self.db = db
        self.uid = uid
        self.password = password
        self.model = model

    def __getattr__(self, method):
        def rpc_method(*args, **kwargs):
            """Return the result of the RPC request."""
            return self.sock.execute(self.db, self.uid, self.password, self.model, method, *args, **kwargs)
        return rpc_method


def _parse_dsn(dsn):
    res = {}
    for i in dsn.split():
        k,v = i.split("=")
        res[k]=v
    return res

# Copy new data from one database (identified via a DSN) to the 'archive' db
# of the current Postgres (as specified by the --db_host, etc)
def archive(args):
    v = ver(args)
    if len(v) < 1 or '9.5' not in v[0]:
        ufload3.progress('Postgres 9.5 is required.')
        return 1

    for dsn in args.from_dsn:
        x = _parse_dsn(dsn)
        if 'dbname' not in x:
            ufload3.progress('DSN is missing dbname.')
            return 1
    
        ufload3.progress("Archive operations_event from %s" % x['dbname'])
        rc, out = _run(args, mkpsql(args, '''
create extension if not exists dblink;
insert into operations_event (instance, kind, time, remote_id, data)
  select * from
    dblink('%s', 'select instance, kind, time, id, data from operations_event') as
    table_name_is_ignored(instance character varying(64),
       kind character varying(64),
       time timestamp without time zone,
       id integer,
       data text)
    on conflict do nothing;''' % (dsn,), 'archive'), get_out=True)
        ufload3.progress(_clean(out))

        ufload3.progress("Archive operations_count from %s" % x['dbname'])
        rc, out = _run(args, mkpsql(args, '''
create extension if not exists dblink;
insert into operations_count (instance, kind, time, count, remote_id)
  select * from
    dblink('%s', 'select instance, kind, time, count, id from operations_count') as
    table_name_is_ignored(instance character varying(64),
       kind character varying(64),
       time timestamp without time zone,
       count integer,
       id integer)
    on conflict do nothing;''' % (dsn,), 'archive'), get_out=True)
        ufload3.progress(_clean(out))

def _clean(out):
    ret = []
    for line in out.split("\n"):
        if line.strip() == "":
            continue
        if line.startswith("NOTICE:"):
            continue
        ret.append(line)
    return "\n".join(ret)


def _zipChecksum(path):
    ufload3.progress("Validating patch checksum")
    with open(path, 'rb') as f:
        contents = f.read()
        # md5 accepts only chunks of 128*N bytes
        md5 = hashlib.md5()
        for i in range(0, len(contents), 8192):
            md5.update(contents[i:i + 8192])
    return md5.hexdigest()


def _zipContents(path):
    ufload3.progress("Reading patch contents")
    with open(path, 'rb') as f:
        return f.read()
    #return contents



def installPatch(args, db='SYNC_SERVER_LOCAL'):
    ufload3.progress("Activating update_client module on %s database" % db)
    #Install the module update_client
    rc = psql(args, "UPDATE ir_module_module SET state = 'installed' WHERE name = 'update_client'", db)
    if rc != 0:
        return rc

    v = args.version
    ufload3.progress("Installing v.%s patch on %s database" % (v, db))

    patch = os.path.normpath(args.patch)

    checksum = _zipChecksum(patch)

    rc, out = psql(args, "SELECT 1 FROM sync_server_version WHERE sum ='{}';".format(checksum), db, True)
    if not out.strip() and rc == 0:
        contents = str(base64.b64encode(_zipContents(patch)), 'utf8')
        netrpc = connect_rpc(args, db)
        version_obj = netrpc.get('sync_server.version.manager')
        v_id = version_obj.create({'name': v, 'importance': 'required', 'comment': v, 'patch': contents})
        version_obj.add_revision([v_id])

        version_line = netrpc.get('sync_server.version')
        line_ids = version_line.search([('sum', '=', checksum)])
        version_line.activate_revision(line_ids)
        return 0
    else:
        ufload3.progress("The v.%s patch on %s database is already installed!!" % (v, db))
        return -1

def installUserRights(args, db='SYNC_SERVER_LOCAL'):
    ufload3.progress('Install user rights : {}'.format(args.user_rights_zip))
    if not args.user_rights_zip or not os.path.isfile(args.user_rights_zip):
        raise ValueError('The file {} not exist'.format(args.user_rights_zip))

    f = open(args.user_rights_zip, 'rb')
    plain_zip = f.read()
    f.close()
    # ur_name = args.user_rights_zip.split('.')[0]
    ur_name, ur_name_extension = os.path.splitext(args.user_rights_zip)
    context= {'run_foreground': True}
    netrpc = connect_rpc(args, db)

    sync_obj = netrpc.get('sync_server.user_rights.add_file')
    # netrpc.config['run_foreground'] = True
    ufload3.progress("Download User Rights")

    load_id = sync_obj.create( {'name': ur_name, 'zip_file': str(base64.b64encode(plain_zip), 'utf8')})
    result = sync_obj.import_zip( [load_id], context)
    result = sync_obj.read( load_id, ['state', 'message'])
    if result['state'] != 'done':
        ufload3.progress('Unable to load UR: %s' % result['message'])
        raise xmlrpc.client.Fault(result['message'])
    else:
        result = sync_obj.done( [load_id])
        ufload3.progress('New UR file loaded')
        return result


    return result


def updateInstance(inst):
    #Call the do_login url in order to trigger the sync (should work even with wrong credentials)
    ufload3.progress("Try to log into instance %s using wrong credentials" % inst)
    urllib.request("http://127.0.0.1:8061/openerp/do_login?target=/&user=ufload&show_password=ufload&db_user_pass=%s" % inst)
    return 0

def set_attchment(args, dbs):
    if args.attachment_path:
        for db in dbs:
            i_name =_run_out(args, mkpsql(args, "select name from res_company", db))
            if i_name and i_name[0]:
                i_path = os.path.join(args.attachment_path, i_name[0].strip())
                if not os.path.exists(i_path):
                    os.mkdir(i_path)
                _run(args, mkpsql(args, "update attachment_config set name='%s';" % (i_path, ), db))

    return True
