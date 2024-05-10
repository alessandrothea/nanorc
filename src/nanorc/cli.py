#!/usr/bin/env python3

"""
Command Line Interface for NanoRC
"""
import os
import sh
import sys
import time
import json
import cmd
import click
import click_shell
from click_shell import make_click_shell
import os.path
import socket
from pathlib import Path
import logging

import threading

from . import __version__

from anytree.resolver import Resolver

from rich.table import Table
from rich.panel import Panel
from rich.console import Console
from rich.traceback import Traceback

from nanorc.runmgr import SimpleRunNumberManager
from nanorc.cfgsvr import FileConfigSaver
from nanorc.core import NanoRC
from nanorc.nano_context import NanoContext
from nanorc.logbook import FileLogbook
from nanorc.credmgr import credentials
from nanorc.rest import RestApi, NanoWebContext, rc_context
from nanorc.webui import WebServer
import nanorc.argval as argval
from nanorc.common_commands import add_common_cmds, add_custom_cmds, accept_timeout, accept_wait, check_rc, execute_cmd_sequence

# ------------------------------------------------------------------------------
# Add -h as default help option
CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])
# ------------------------------------------------------------------------------

loglevels = {
    'CRITICAL': logging.CRITICAL,
    'ERROR': logging.ERROR,
    'WARNING': logging.WARNING,
    'INFO': logging.INFO,
    'DEBUG': logging.DEBUG,
    'NOTSET': logging.NOTSET,
}

def updateLogLevel(loglevel):
        level = loglevels[loglevel]

        # Update log level for root logger
        logger = logging.getLogger()
        logger.setLevel(level)
        for handler in logger.handlers:
            handler.setLevel(level)
        # And then manually tweak 'sh.command' logger. Sigh.
        sh_command_level = level if level > logging.INFO else (level+10)
        sh_command_logger = logging.getLogger(sh.__name__)
        # sh_command_logger.propagate = False
        sh_command_logger.setLevel(sh_command_level)

        # Can't deal with that much logging, K8s!
        import kubernetes.client.rest as k8srest
        k8s_command_level = logging.INFO
        k8s_command_logger = logging.getLogger(k8srest.__name__)
        # sh_command_logger.propagate = False
        k8s_command_logger.setLevel(k8s_command_level)

        for handler in sh_command_logger.handlers:
            handler.setLevel(sh_command_level)


# ------------------------------------------------------------------------------
@click_shell.shell(prompt='shonky rc> ', chain=True, context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__)
@click.option('-t', '--traceback', is_flag=True, default=False, help='Print full exception traceback')
@click.option('-l', '--loglevel', type=click.Choice(loglevels.keys(), case_sensitive=False), default='INFO', help='Set the log level')
@click.option('--cfg-dumpdir', type=click.Path(), default="./", help='Path where the config gets copied on start')
@click.option('--log-path', type=click.Path(exists=True), default=None, help='Where the logs should go (on localhost of applications)')
@click.option('--kerberos/--no-kerberos', default=True, help='Whether you want to use kerberos for communicating between processes')
@click.option('--logbook-prefix', type=str, default="./", help='Prefix for the logbook file')
@click.option('--pm', type=str, default="ssh://", help='Process manager, can be: ssh://, kind://, or k8s://np04-srv-015:31000, for example', callback=argval.validate_pm)
@click.option('--web/--no-web', is_flag=True, default=False, help='whether to spawn webui')
@click.option('--tui/--no-tui', is_flag=True, default=False, help='whether to use TUI')
@accept_timeout(60)
@click.option('--partition-number', type=int, default=0, help='Which partition number to run', callback=argval.validate_partition_number)
@click.option('--web/--no-web', is_flag=True, default=False, help='whether to spawn WEBUI')
@click.argument('top_cfg', type=str, callback=argval.validate_conf)
@click.argument('partition-label', type=str, callback=argval.validate_partition)
@click.pass_obj
@click.pass_context
def cli(ctx, obj, traceback, loglevel, cfg_dumpdir, log_path, logbook_prefix, timeout, kerberos, partition_number, web, top_cfg, partition_label, tui, pm):
    obj.print_traceback = traceback
    credentials.user = 'user'
    ctx.command.shell.prompt = f'{credentials.user}@rc> '


    grid = Table(title='Shonky NanoRC', show_header=False, show_edge=False)
    grid.add_column()
    grid.add_row("This is an admittedly shonky nano RC to control DUNE-DAQ applications.")
    grid.add_row("  Give it a command and it will do your biddings,")
    grid.add_row("  but trust it and it will betray you!")
    grid.add_row(f"Use it with care, {credentials.user}!")

    if not tui:
        obj.console.print(Panel.fit(grid))

    port_offset = 0 + partition_number * 500
    rest_port = 5005 + partition_number
    webui_port = 5015 + partition_number

    if loglevel:
        updateLogLevel(loglevel)

    rest_thread  = threading.Thread()
    webui_thread = threading.Thread()
    if web and tui:
        raise RuntimeError("cant have TUI and GUI at the same time")
    try:
        rc = NanoRC(
            console = obj.console,
            top_cfg = top_cfg,
            run_num_mgr = SimpleRunNumberManager(),
            run_registry = FileConfigSaver(cfg_dumpdir),
            logbook_type = "file",
            timeout = timeout,
            use_kerb = kerberos,
            partition_label = partition_label,
            logbook_prefix = logbook_prefix,
            pm = pm,
            port_offset = port_offset
        )

        if log_path:
            rc.log_path = os.path.abspath(log_path)

        add_common_cmds(ctx.command)
        add_custom_cmds(ctx, rc.execute_custom_command, rc.custom_cmd, rc.status)

        if web or tui:                  #The TUI also needs the rest thread
            host = socket.gethostname()

            rc_context.obj = obj
            rc_context.console = obj.console
            rc_context.top_json = top_cfg
            rc_context.rc = rc
            rc_context.commands = ctx.command.commands
            rc_context.ctx = ctx

            obj.console.log(f"Starting up RESTAPI on {host}:{rest_port}")
            rest = RestApi(rc_context, host, rest_port)
            rest_thread = threading.Thread(target=rest.run, name="NanoRC_REST_API")
            rest_thread.start()
            obj.console.log(f"Started RESTAPI")

            if web:
                webui_thread = None
                obj.console.log(f'Starting up Web UI on {host}:{webui_port}')
                webui = WebServer(host, webui_port, host, rest_port)
                webui_thread = threading.Thread(target=webui.run, name='NanoRC_WebUI')
                webui_thread.start()
                obj.console.log(f"")
                obj.console.log(f"")
                obj.console.log(f"")
                obj.console.log(f"")
                grid = Table(title='Web NanoRC', show_header=False, show_edge=False)
                grid.add_column()
                grid.add_row(f"Started Web UI, you can now connect to: [blue]{host}:{webui_port}[/blue],")
                if 'np04' in host:
                    grid.add_row(f"You probably need to set up a SOCKS proxy to lxplus:")
                    grid.add_row("[blue]ssh -N -D 8080 your_cern_uname@lxtunnel.cern.ch[/blue] # on a different terminal window on your machine")
                    grid.add_row(f'Make sure you set up browser SOCKS proxy with port 8080 too,')
                    grid.add_row('on Chrome, \'Hotplate localhost SOCKS proxy setup\' works well).')
                elif 'lxplus' in host:
                    grid.add_row(f"You probably need to set up a SOCKS proxy to lxplus:")
                    grid.add_row(f"[blue]ssh -N -D 8080 your_cern_uname@{host}[/blue] # on a different terminal window on your machine")
                    grid.add_row(f'Make sure you set up browser SOCKS proxy with port 8080 too,')
                    grid.add_row('on Chrome, \'Hotplate localhost SOCKS proxy setup\' works well).')
                grid.add_row()
                grid.add_row(f'[red]To stop this, ctrl-c [/red][bold red]twice[/bold red] (that will kill the REST and WebUI threads).')
                obj.console.print(Panel.fit(grid))
                obj.console.log(f"")
                obj.console.log(f"")
                obj.console.log(f"")
                obj.console.log(f"")


    except Exception as e:
        logging.getLogger("cli").exception("Failed to build NanoRC")
        raise click.Abort()

    def cleanup_rc():
        rc.quit()
        if rc.topnode.state != 'none':
            logging.getLogger("cli").warning("NanoRC context cleanup: Aborting applications before exiting")
            rc.abort(timeout=120)
        if rc.return_code:
            ctx.exit(rc.return_code)

    ctx.call_on_close(cleanup_rc)
    obj.rc = rc
    obj.shell = ctx.command
    rc.ls(False)

    if web:
        rest_thread.join()
        webui_thread.join()

    if tui:
        from .tui import NanoRCTUI
        tui = NanoRCTUI(host=host, rest_port=rest_port, timeout=rc.timeout, banner=Panel.fit(grid))
        tui.run()
        cleanup_rc()
        ctx.exit(rc.return_code)



################################
########### Commands ###########
################################

def add_run_start_parameters():
    # sigh start...
    def add_decorator(function):
        f1 = click.argument('run_num', type=int)(function)
        f2 = click.option('--trigger-rate', type=float, default=None, help='Trigger rate (Hz)')(f1)
        f3 = click.option('--disable-data-storage/--enable-data-storage', type=bool, default=False, help='Toggle data storage')(f2)
        f4 = accept_timeout(None)(f3)
        f5 = click.option('--run-type', type=click.Choice(['TEST', 'PROD']), default='TEST')(f4)
        return click.option('--message', type=str, default="")(f5)
     # sigh end
    return add_decorator

def start_defaults_overwrite(kwargs):
    kwargs['ignore_run_registry_insertion_error'] = True
    #kwargs['run_type'] = 'TEST'
    kwargs['path'] = None
    return kwargs

from nanorc.common_commands import  accept_message

@cli.command('message')
@accept_message(argument=True)
@click.pass_obj
def message(obj, message):
    obj.rc.message(message)



@cli.command('start_run')
@add_run_start_parameters()
@accept_wait()
@click.pass_obj
@click.pass_context
def start_run(ctx, obj, wait:int, **kwargs):
    obj.rc.run_num_mgr.set_run_number(kwargs['run_num'])
    kwargs['node_path'] = None

    execute_cmd_sequence(
        ctx = ctx,
        rc = obj.rc,
        command = 'start_run',
        wait = wait,
        force = False,
        cmd_args = start_defaults_overwrite(kwargs)
    )


@cli.command('start')
@add_run_start_parameters()
@click.pass_obj
@click.pass_context
def start(ctx, obj:NanoContext, **kwargs):
    obj.rc.run_num_mgr.set_run_number(kwargs['run_num'])
    obj.rc.start(
        **start_defaults_overwrite(kwargs)
    )
    check_rc(ctx,obj.rc)
    obj.rc.status()

