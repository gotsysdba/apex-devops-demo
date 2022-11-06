#!/bin/env python3
import argparse, logging, subprocess, os, sys, glob, zipfile, re
from datetime import datetime

# Logging Default
level    = logging.INFO
format   = '[%(asctime)s] %(levelname)8s: %(message)s'
handlers = [logging.StreamHandler()]
datefmt  = '%Y-%b-%d %H:%M:%S'
logging.basicConfig(level = level, format = format, handlers = handlers, datefmt=datefmt)
log = logging.getLogger(__name__)

""" Helpler Functions
"""
def upd_sqlnet(wallet):
    """ This processes an ADB wallet and updates the sqlnet.ora file
        Normally could use 'set cloudconfig' but a bug in 22.3 put a stop to that
    """
    tns_admin = os.path.dirname(os.path.abspath(wallet))
    with zipfile.ZipFile(wallet, 'r') as zip_ref:
        zip_ref.extractall(tns_admin)

    with open(os.path.join(tns_admin,'sqlnet.ora')) as file:
        s = file.read()
        s = s.replace('DIRECTORY="?/network/admin"', 'DIRECTORY="'+tns_admin+'"')
    with open(os.path.join(tns_admin,'sqlnet.ora'), "w") as file:
        file.write(s)

def pre_generate(directory, remove_controller=False):
    """ In sqlcl 22+ the naming of files was changed to support using the generate extension in core liquibase. 
        In core liquibase there is currently no way to inject a ChangeLogSyncListener and since they mandated 
        the sqlcl extension work in core liquibase it can not overwrite the files like it did in pre-22. 
        So... all files generated by sqlcl+lb will be removed first, then regenerated.  This should not cause
        a version control issue as the re-generation will produce an exact replica of what was there.
    """
    log.info(f'Cleaning up {directory}...')
    for file in glob.iglob(f'{directory}/**/*.xml', recursive=True):
        log.debug(f'Processing {file}')
        if file.startswith(f'{directory}/controller') and remove_controller:
            log.info(f'Removing {file} for regeneration')
            os.remove(file)
            continue
        for line in open(file, "r"):
            if re.search("\<changeSet.*author=\"\(.*\)-Generated\".*?", line):
                log.info(f'Removing {file} for regeneration')
                os.remove(file)
                continue

def post_generate(directory):
    """ sqlcl may create modifications where they don't exist; specifically blank lines
        this function is to clean those to keep git happy
    """
    log.info(f'Cleaning up {directory}...')
    for file in glob.iglob(f'{directory}/**/*.xml', recursive=True):
        log.debug(f'Processing {file}')
        with open(file) as reader, open(file, 'r+') as writer:
            for line in reader:
                if line.strip():
                    writer.write(line)
            writer.truncate()

def run_sqlcl(run_as, password, service, path, cmd, tns_admin):
    lb_env = os.environ.copy()
    lb_env['password']  = password
    lb_env['TNS_ADMIN'] = tns_admin

    # Keep password off the command line/shell history
    sql_cmd = f'''
        conn {run_as}/{password}@{service}_high
        {cmd}
    '''

    log.debug(f'Running: {sql_cmd}')
    result = subprocess.run(['sql', '/nolog'], universal_newlines=True, cwd=f'./{path}', input=f'{sql_cmd}', env=lb_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    exit_status = 0
    error_matches = ['Error Message','ORA-','SQL Error','Validation Failed','Unexpected internal error']
    result_list = result.stdout.splitlines();
    for line in filter(None, result_list):
        log.info(line)
        if any(x in line for x in error_matches):
            exit_status = 1
    if result.returncode or exit_status:
        log.fatal('Exiting...')
        sys.exit(1)

    log.info('SQLcl command successful')

def deploy_call(path, user, password, tns_admin, args):
    if os.path.exists(os.path.join(path, 'controller.xml')):
        log.info(f'Running {path}/controller.xml as {user}')
        cmd = f'lb update -changelog-file controller.xml;'
        run_sqlcl(user, password, args.dbName, path, cmd, tns_admin)

""" Action Functions
"""
def deploy(password, tns_admin, args):
    deploy_call('admin', 'ADMIN', password, tns_admin, args)
    deploy_call('schema', f'ADMIN[{args.dbUser}]', password, tns_admin, args)
    deploy_call('data', f'ADMIN[{args.dbUser}]', password, tns_admin, args)
    deploy_call('apex', f'ADMIN[{args.dbUser}]', password, tns_admin, args)   

def generate(password, tns_admin, args):
    ## Generate Schema
    pre_generate('schema', True)
    log.info('Starting schema export...')
    cmd = 'lb generate-schema -split -grants -runonchange -fail-on-error'  
    run_sqlcl(f'ADMIN[{args.dbUser}]', password, args.dbName, 'schema', cmd, tns_admin)
    post_generate('schema')


    ## Generate APEX
    pre_generate('apex', False)
    log.info('Starting apex export...')
    cmd = 'lb generate-apex-object -applicationid 103 -expaclassignments true -expirnotif true -exporiginalids true -exppubreports true -expsavedreports true -exptranslations true -skipexportdate true'
    run_sqlcl(f'ADMIN[{args.dbUser}]', password, args.dbName, 'apex', cmd, tns_admin)
    post_generate('apex')

def destroy(password, tns_admin, args):
    cmd = 'lb rollback-count -changelog controller.xml -count 999;'
    run_sqlcl('ADMIN', password, args.dbName, 'admin', cmd, tns_admin)
    
""" INIT
"""
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='CI/CD Liquibase Helper')
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument('--dbName',   required=True,  action='store',      help='Database Name')
    parent_parser.add_argument('--dbUser',   required=True,  action='store',      help='Schema User')
    parent_parser.add_argument('--dbPass',   required=False, action='store',      help='ADMIN Password')
    parent_parser.add_argument('--dbWallet', required=False, action='store',      help='Database Wallet')
    parent_parser.add_argument('--debug',    required=False, action='store_true', help='Enable Debug')

    subparsers = parser.add_subparsers(help='Actions')
    # Deploy
    deploy_parser = subparsers.add_parser('deploy', parents=[parent_parser], 
        help='Deploy'
    )
    deploy_parser.set_defaults(func=deploy,action='deploy')

    # Generate 
    generate_parser = subparsers.add_parser('generate', parents=[parent_parser], 
        help='Generate Changelogs'
    )
    generate_parser.set_defaults(func=generate,action='generate')

    # Destroy
    destroy_parser = subparsers.add_parser('destroy', parents=[parent_parser], 
        help='Destroy'
    )
    destroy_parser.set_defaults(func=destroy,action='destroy')    

    if len(sys.argv[1:])==0:
        parser.print_help()
        parser.exit()

    args = parser.parse_args()

    if args.debug:
        log.getLogger().setLevel(logging.DEBUG)
        log.debug("Debugging Enabled")

    log.debug('Arguments: {}'.format(args))

    """ MAIN
    """
    if args.dbPass:
        password = args.dbPass
    else:
        try:
            f = open(".secret", "r")
            password = f.readline().split()[-1]
        except:
            log.fatal('Database password required')
            sys.exit(1)
            
    # If a wallet was provided, extract and use, otherwise we expect TNS_ADMIN to be set
    if args.dbWallet:
        upd_sqlnet(args.dbWallet)
        tns_admin = os.path.dirname(os.path.abspath(args.dbWallet))
    else:
        try:
            tns_admin = os.environ['TNS_ADMIN']
        except:
            log.fatal('Wallet not specified and TNS_ADMIN not set, unable to proceed with DB resolution')
            sys.exit(1)
        if not os.path.exists(f'{tns_admin}/tnsnames.ora'):
            log.fatal('{tns_admin}/tnsnames.ora not found, unable to proceed with DB resolution')
            sys.exit(1)

    args.func(password, tns_admin, args)
    sys.exit(0)