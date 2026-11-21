"""Install the fixed root worker launcher once, as administrator (Python 3.8+)."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile


def install(path, text, mode):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.'+path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, 0)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', default=os.environ.get('SUDO_USER'))
    parser.add_argument('--python', required=True, help='Absolute Python 3.11+ executable used by Squad')
    parser.add_argument('--data-root', help='Authorized Squad data tree; defaults to USER_HOME/.aster-squad')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo as administrator')
    if not args.user or not re.fullmatch('[a-z_][a-z0-9_-]*', args.user):
        parser.error('Specify the Squad host user with --user')
    user = pwd.getpwnam(args.user)
    if user.pw_uid == 0:
        parser.error('Squad controller must run as a non-root host user')
    python = Path(args.python).absolute()
    if not python.is_file():
        parser.error('Worker Python executable does not exist')
    root = Path(args.data_root or Path(user.pw_dir)/'.aster-squad').resolve()
    # Pin the trusted host environment's import paths, including user-installed
    # dependencies. Do not accept PYTHONPATH from an agent's startup environment.
    paths = json.loads(subprocess.check_output(['/usr/sbin/runuser','-u',args.user,'--',str(python),'-P','-c',
        'import json,sys; assert sys.version_info >= (3,11); print(json.dumps(sys.path))'],
        env={'HOME':user.pw_dir,'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},text=True))
    helper = Path('/usr/local/libexec/aster-squad-root-worker')
    settings = Path('/etc/aster-squad/root-worker.json')
    rule = Path('/etc/sudoers.d/aster-squad-root-worker')
    visudo = shutil.which('visudo') or '/usr/sbin/visudo'
    text = args.user+' ALL=(root) NOPASSWD: '+str(helper)+' *\n'
    with tempfile.NamedTemporaryFile(mode='w', prefix='squad-sudoers-') as f:
        f.write(text);f.flush()
        subprocess.run([visudo,'-cf',f.name],check=True)
    helper.parent.mkdir(parents=True,exist_ok=True)
    settings.parent.mkdir(parents=True,exist_ok=True)
    # Source is copied, not symlinked into a writable checkout.
    install(helper,'#!/usr/bin/python3\n'+(Path(__file__).resolve().parents[1]/'squad/root_worker.py').read_text(),0o755)
    install(settings,json.dumps({'uid':user.pw_uid,'gid':user.pw_gid,'python':str(python),'pythonpath':[p for p in paths if p],'data_root':str(root)})+'\n',0o644)
    install(rule,text,0o440)
    print('Root worker launcher installed. No agent has been granted root by this installation.')


if __name__ == '__main__':main()
