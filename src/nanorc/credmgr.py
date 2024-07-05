


def env_for_kerberos(ticket_dir):
    import os
    ticket_dir = os.path.expanduser(ticket_dir)
    env = {'KRB5CCNAME': f'DIR:{ticket_dir}'}
    return env


def new_kerberos_ticket(user:str, realm:str, password:str=None, ticket_dir:str="~/"):
    env = env_for_kerberos(ticket_dir)
    success = False
    password_provided = password is not None

    while not success:
        import subprocess

        p = subprocess.Popen(
            ['kinit', f'{user}@{realm}'],
            stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env
        )

        if p.poll() is not None and p.returncode!=0:
          raise RuntimeError(f'Could not execute kinit {user}@{realm}\n{stdout_data[-1].decode()}')

        if password is None:
            print(f'Password for {user}@{realm}:')
            try:
                from getpass import getpass
                password = getpass()

            except KeyboardInterrupt:
                print()
                return False

        stdout_data = p.communicate(password.encode())
        print(stdout_data[-1].decode())

        if not password_provided:
            password = None

        success = p.returncode==0

        if not success and password_provided:
            raise RuntimeError(f'Authentication error for {user}@{realm}. The password provided (likely in configuration file) is incorrect')

    return True



def get_kerberos_user(silent=False, ticket_dir:str="~/"):
    import logging
    log = logging.getLogger('get_kerberos_user')

    env = env_for_kerberos(ticket_dir)
    args=['klist'] # on my mac, I can specify --json and that gives everything nicely in json format... but...
    import subprocess

    proc = subprocess.run(args, capture_output=True, text=True, env=env)
    raw_kerb_info = proc.stdout.split('\n')


    if not silent:
        log.info(proc.stdout)

    kerb_user = None
    for line in raw_kerb_info:
        split_line = line.split(' ')
        split_line =  [x for x in split_line if x!='']
        find_princ = line.find('Default principal')
        if find_princ!=-1:
            kerb_user = split_line[2]
            kerb_user = kerb_user.split('@')[0]

        if kerb_user:
            return kerb_user
    return None



def check_kerberos_credentials(against_user:str, silent=False, ticket_dir:str="~/"):
    import logging
    log = logging.getLogger('check_kerberos_credentials')

    env = env_for_kerberos(ticket_dir)

    kerb_user = get_kerberos_user(
        silent=silent,
        ticket_dir=ticket_dir
    )

    if not silent:
        if kerb_user:
            log.info(f'Detected kerberos ticket for user: \'{kerb_user}\'')
        else:
            log.info(f'No kerberos ticket found')

    if not kerb_user:
        if not silent: log.info('No kerberos ticket')
        return False
    elif kerb_user != against_user: # we enforce the user is the same
        if not silent: log.info('Another user is logged in')
        return False
    else:
        import subprocess
        ticket_is_valid = subprocess.call(['klist', '-s'], env=env) == 0
        if not silent and not ticket_is_valid:
            log.info('Kerberos ticket is expired')
        return ticket_is_valid



class ServiceAuthentication():
    def __init__(self, service:str, username:str):
        super().__init__()
        self.service = service
        self.username = username


class SimpleAuthentication(ServiceAuthentication):
    def __init__(self, service:str, username:str, password:str):
        super().__init__(service, username)
        self.password = password


class ServiceAccountWithKerberos(ServiceAuthentication):
    def __init__(self, service:str, username:str, password:str, realm:str):
        super().__init__(service, username)
        self.password = password
        self.realm = realm

    def generate_cern_sso_cookie(self, website, kerberos_directory, output_directory):
        args = []
        env = {'KRB5CCNAME': f'DIR:{kerberos_directory}'}


        from nanorc.utils import which
        import sh

        if which('cern-get-sso-cookie'):
            executable = sh.Command("cern-get-sso-cookie")
            args = ["--krb", "-r", "-u", website, "-o", output_directory]
        elif which('auth-get-sso-cookie'):
            executable = sh.Command('auth-get-sso-cookie')
            args = ['-u', website, '-o', output_directory]
        else:
            raise RuntimeError("Couldn't get SSO cookie, there is no 'cern-get-sso-cookie' or 'auth-get-user-token' on your system!")

        proc = executable(*args, _env=env, _new_session=True)
        if proc.exit_code != 0:
            self.log.error("Couldn't get SSO cookie!")
            self.log.error("You need to 'kinit' or 'change_user' and try again!")
            self.log.error(f'{executable} stdout: {proc.stdout}')
            self.log.error(f'{executable} stderr: {proc.stderr}')
            raise RuntimeError("Couldn't get SSO cookie!")
        return output_directory


class UserAccountWithKerberos(ServiceAuthentication):
    def __init__(self, service:str, username:str, realm:str):
        super().__init__(service, username)
        self.realm = realm

    def __eq__(self, other):
        return self.service == other.service and self.realm == other.realm and self.username == other.username

class AuthenticationFactory():
    @staticmethod
    def get_from_dict(service:str, auth_data:dict[str,str]):
        match auth_data['type']:
            case "simple":
                return SimpleAuthentication(
                    service,
                    auth_data['user'],
                    auth_data.get('password'),
                )
            case "service-account":
                return ServiceAccountWithKerberos(
                    service,
                    auth_data['user'],
                    auth_data.get('password'),
                    auth_data['realm'],
                )
            case _:
                raise RuntimeError(f"Authentication method {auth_data['type']} is not supported")



class CredentialManager:
    def __init__(self):
        import logging
        self.log = logging.getLogger(self.__class__.__name__)
        self.authentications = []

    def get_nanorc_username(self):
        user = self.get_login(service='nanorc')
        if user:
            return user.username
        else:
            import getpass
            return getpass.getuser()

    def add_login(self, service:str, data:dict[str,str]):
        self.authentications.append(
            AuthenticationFactory.get_from_dict(service, data)
        )


    def get_login(self, service:str, user:str=".*"):
        import re

        for auth in self.authentications:
            if re.search(service, auth.service) and re.search(user, auth.username):
                return auth
        self.log.error(f"Couldn't find login for service: {service}, user: {user}")


    def rm_login(self, service:str, user:str=".*"):
        import re

        from copy import deepcopy as dc
        auths = dc(self.authentications)

        for auth in auths:
            if re.search(service, auth.service) and re.search(user, auth.username):
                self.log.info(f"Removing: '{auth.service}', user: '{auth.username}' from credential manager")
                self.authentications.remove(auth)

credentials = CredentialManager()


class CERNSessionHandler:
    def __init__(self, console, apparatus_id:str, session_number:int, username:str):
        self.console = console
        import logging
        self.log = logging.getLogger(self.__class__.__name__)
        self.nanorc_user = UserAccountWithKerberos(
            service='nanorc',
            username=username,
            realm="CERN.CH",
        )

        self.session_name = apparatus_id
        self.session_number = session_number
        self.start_session()

        if not self.authenticate_nanorc_user():
            self.log.error(f'User \'{username}\' cannot authenticate, exiting...')
            exit(1)

    @staticmethod
    def __get_session_kerberos_cache_path(session_name:str, session_number:int):
        import os
        from pathlib import Path

        return Path(
            os.path.expanduser(f'~/.nanorc_userkerbcache_{session_name}_session_{session_number}')
        )

    @staticmethod
    def session_is_active(session_name:str, session_number:int):
        cache_path = CERNSessionHandler.__get_session_kerberos_cache_path(
            session_name,
            session_number,
        )
        import os
        return os.path.isfile(cache_path/'active_session')

    @staticmethod
    def get_kerberos_user_from_session(session_name:str, session_number:int):
        cache = CERNSessionHandler.__get_session_kerberos_cache_path(
            session_name,
            session_number,
        )
        return get_kerberos_user(
            silent=True,
            ticket_dir = cache,
        )

    def klist(self):
        # HACK
        from subprocess import Popen
        from subprocess import STDOUT, PIPE
        from nanorc.credmgr import env_for_kerberos, CERNSessionHandler

        env = env_for_kerberos(
            CERNSessionHandler.__get_session_kerberos_cache_path(
                self.session_name, self.session_number
            )
        )

        cmd = ['klist']
        printout = ''
        for k,v in env.items():
            printout += f'{k}=\"{v}\" '

        printout += ' '.join(cmd)
        print(printout+"\n")

        with Popen(
            cmd,
            stderr=STDOUT,
            stdout=PIPE,
            bufsize=1,
            text=True,
            env=env
        ) as sp:
            for line in sp.stdout:
                print(line.replace("\n", ""))



    def authenticate_nanorc_user(self):
        session_kerb_cache = CERNSessionHandler.__get_session_kerberos_cache_path(
            self.session_name,
            self.session_number,
        )
        import os

        if not os.path.isdir(session_kerb_cache):
            os.mkdir(session_kerb_cache)

        if self.nanorc_user_is_authenticated():
            # we're authenticated, stop here
            return True

        return new_kerberos_ticket(
            user = self.nanorc_user.username,
            realm = self.nanorc_user.realm,
            password = None,
            ticket_dir = session_kerb_cache,
        )

    def nanorc_user_is_authenticated(self):
        session_kerb_cache = CERNSessionHandler.__get_session_kerberos_cache_path(
            self.session_name,
            self.session_number,
        )
        return check_kerberos_credentials(
            against_user = self.nanorc_user.username,
            silent = True,
            ticket_dir = session_kerb_cache,
        )

    def stop_session(self):
        import os
        session_active_marker = CERNSessionHandler.__get_session_kerberos_cache_path(self.session_name, self.session_number)/'active_session'
        if os.path.isfile(session_active_marker):
            os.remove(session_active_marker)

    def quit(self):
        if self.session_name is not None and self.session_number is not None:
            self.stop_session()


    def create_session_kerberos_cache(self):
        if self.session_number is None or self.session_name is None:
            raise RuntimeError('Session number (or name) hasn\'t been specified, cannot create a session kerberos cache')

        user_kerb_cache = CERNSessionHandler.__get_session_kerberos_cache_path(self.session_name, self.session_number)
        import os

        if not os.path.isdir(user_kerb_cache):
            os.mkdir(user_kerb_cache)


    def start_session(self):
        in_use = CERNSessionHandler.session_is_active(
            session_name = self.session_name,
            session_number = self.session_number,
        )

        if in_use:
            kuser = CERNSessionHandler.get_kerberos_user_from_session(
                session_name = self.session_name,
                session_number = self.session_number,
            )

            if kuser is not None and kuser != self.nanorc_user.username:
                self.console.print(f'[bold red]Session #{self.session_number} on apparatus \'{self.session_name}\' seems to be used by \'{kuser}\', do you want to steal it? Y/N[/bold red]')
            else:
                self.console.print(f'[bold red]You seem to already have session #{self.session_number} on apparatus \'{self.session_name}\' active, are you sure you want to proceed? Y/N[/bold red]')

            while True:
                try:
                    steal = input().upper()
                except KeyboardInterrupt:
                    self.console.print(f'Exiting...')
                    exit(1)
                if   steal == 'Y': break
                elif steal == 'N': exit(0)
                self.console.print(f'[bold red]Wrong answer! Y or N?[/bold red]')


        self.create_session_kerberos_cache()
        cache_path = CERNSessionHandler.__get_session_kerberos_cache_path(
            self.session_name,
            self.session_number,
        )

        f = open(cache_path/'active_session', "w")
        f.close()


    def change_user(self, user:UserAccountWithKerberos):

        if user == self.nanorc_user.username:
            return True

        previous_user = self.nanorc_user
        self.nanorc_user = UserAccountWithKerberos(
            service = previous_user.service,
            username = user,
            realm = previous_user.realm,
        )

        if not self.authenticate_nanorc_user():
            self.nanorc_user = previous_user
            return False

        return True




