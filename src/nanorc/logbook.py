from elisa_client_api.elisa import Elisa
from elisa_client_api.searchCriteria import SearchCriteria
from elisa_client_api.messageInsert import MessageInsert
from elisa_client_api.messageReply import MessageReply
from elisa_client_api.exception import ElisaError

import logging
import os.path
import subprocess
import copy
import time
import requests
from .credmgr import credentials

class FileLogbook:
    def __init__(self, path:str, console):
        self.path = path
        self.file_name = f"{path}logbook.txt"
        self.website = self.file_name
        self.console = console
        self.run_num = ""
        self.run_type = ""

    def now(self):
        from datetime import datetime
        now = datetime.now() # current date and time
        return now.strftime("%Y-%m-%d--%H-%M-%S")

    def message_on_start(self, messages:str, session:str, run_num:int, run_type:str):
        self.run_num = run_num
        self.run_type = run_type
        self.website = self.file_name
        f = open(self.file_name, "a")
        f.write(f"{self.now()}: User started a run {self.run_num}, of type {self.run_type} on {session}\n")
        f.write(f'{self.now()}: {messages}\n')
        f.close()

    def add_message(self, messages:str, session:str):
        f = open(self.file_name, "a")
        f.write(f'{self.now()}: {messages}\n')
        f.close()

    def message_on_stop(self, messages:str, session:str):
        f = open(self.file_name, "a")
        f.write(f"{self.now()} User stopped the run {self.run_num}, of type {self.run_type} on {session}\n")
        f.write(f'{self.now()}: {messages}\n')
        f.close()



class ElisaHandler:
    def __init__(self, socket, session_handler):
        self.socket = socket
        self.session_handler = session_handler
        self.log = logging.getLogger(self.__class__.__name__)
        self.log.info(f'Connected to ELisA logbook at {self.socket}')
        auth = credentials.get_login("elisa_logbook")
        self.API_USER=auth.username
        self.API_PSWD=auth.password

    def _start_new_message_thread(self):
        self.log.info("ELisA logbook: Next message will be a new thread")
        self.current_id = None
        self.current_run = None
        self.current_run_type = None


    def _send_message(self, subject:str, body:str, command:str):
        user = self.session_handler.nanorc_user.username
        data = {'author':user, 'title':subject, 'body':body, 'command':command, 'systems':["daq"]}
        try:
            if not self.current_id:
                r = requests.post(f'{self.socket}/v1/elisaLogbook/new_message/', auth=(self.API_USER, self.API_PSWD), json=data)
            else:
                data["id"] = self.current_id
                r = requests.put(f'{self.socket}/v1/elisaLogbook/reply_to_message/', auth=(self.API_USER, self.API_PSWD), json=data)
            r.raise_for_status()
            response = r.json()
            if r.status_code != 201:
                self.log.error(f'Exception thrown while inserting data in elisa:')
                e = (response['response'])
                self.log.error(e)
                import logging
                if logging.DEBUG >= logging.root.level:
                    self.console.print_exception()
                raise e
            else:
                self.current_id = response['thread_id']
                self.log.info(f"ELisA logbook: Sent message (ID{self.current_id})")
        except requests.HTTPError as exc:
            error = f"of HTTP Error (maybe failed auth, maybe ill-formed post message, ...)"
            self.log.error(error)
        except requests.ConnectionError as exc:
            error = f"connection to {self.API_SOCKET} wasn't successful"
            self.log.error(error)
        except requests.Timeout as exc:
            error = f"connection to {self.API_SOCKET} timed out"
            self.log.error(error)

    def message_on_start(self, messages:[str], session:str, run_num:int, run_type:str):
        self._start_new_message_thread()
        self.current_run_num = run_num
        self.current_run_type = run_type


        text = ''
        for message in messages:
            text += f"\n<p>{message}</p>"
        text += "\n<p>log automatically generated by NanoRC.</p>"

        title = f"Run {self.current_run_num} ({self.current_run_type}) started on {session}"
        self._send_message(subject=title, body=text, command='start')


    def add_message(self, messages:[str], session:str):

        for message in messages:
            text = f"<p>{message}</p>"
            self._send_message(subject="User comment", body=text, command='message')


    def message_on_stop(self, messages:[str], session:str):
        text = ''

        for message in messages:
            text = f"\n<p>{message}</p>"

        title = f"Run {self.current_run_num} ({self.current_run_type}) stopped on {session}"
        text += title
        text += "\n<p>log automatically generated by NanoRC.</p>"

        self._send_message(subject=title, body=text, command='stop')
