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



class ElisaLogbook:
    def __init__(self, console, configuration, session_handler):
        self.console = console
        self.session_handler = session_handler
        self.elisa_arguments = {"connection": configuration['connection']}
        self.website = configuration['website']
        self.message_attributes = configuration['attributes']
        self.log = logging.getLogger(self.__class__.__name__)
        self.log.info(f'ELisA logbook connection: {configuration["website"]} (API: {configuration["connection"]})')

    def _start_new_message_thread(self):
        self.log.info("ELisA logbook: Next message will be a new thread")
        self.current_id = None
        self.current_run = None
        self.current_run_type = None


    def _send_message(self, subject:str, body:str, command:str):
        user = self.session_handler.nanorc_user.username

        elisa_arg = copy.deepcopy(self.elisa_arguments)

        elisa_user = credentials.get_login('elisa')

        import tempfile
        with tempfile.NamedTemporaryFile() as tf:
            try:
                sso = {"ssocookie": self.session_handler.generate_elisa_cern_cookie(self.website, tf.name)}
                elisa_arg.update(sso)
                elisa_inst = Elisa(**elisa_arg)
                answer = None
                if not self.current_id:
                    self.log.info("ELisA logbook: Creating a new message thread")
                    message = MessageInsert()
                    message.author = user
                    message.subject = subject
                    for attr_name, attr_data in self.message_attributes[command].items():
                        if attr_data['set_on_new_thread']:
                            setattr(message, attr_name, attr_data['value'])
                    message.systemsAffected = ["DAQ"]
                    message.body = body
                    answer = elisa_inst.insertMessage(message)

                else:
                    self.log.info(f"ELisA logbook: Answering to message ID{self.current_id}")
                    message = MessageReply(self.current_id)
                    message.author = user
                    message.systemsAffected = ["DAQ"]
                    for attr_name, attr_data in self.message_attributes[command].items():
                        if attr_data['set_on_reply']:
                            setattr(message, attr_name, attr_data['value'])
                    message.body = body
                    answer = elisa_inst.replyToMessage(message)
                self.current_id = answer.id

            except ElisaError as ex:
                self.log.error(f"ELisA logbook: {str(ex)}")
                self.log.error(answer)
                raise ex

            except Exception as e:
                self.log.error(f'Exception thrown while inserting data in elisa:')
                self.log.error(e)
                import logging
                if logging.DEBUG >= logging.root.level:
                    self.console.print_exception()
                raise e

            self.log.info(f"ELisA logbook: Sent message (ID{self.current_id})")



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
