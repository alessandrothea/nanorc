from .statefulnode import StatefulNode
from .node import ApplicationNode, SubsystemNode
from .k8spm import K8sProcess
from anytree import RenderTree, PreOrderIter
from rich.panel import Panel
import logging as log
from rich.console import Console
from rich.table import Table
from rich.text import Text
import sh

def status_data(node, get_children=True) -> dict:
    ret = {}
    if isinstance(node, ApplicationNode):
        sup = node.sup
        if sup.desc.proc.is_alive():
            ret['process_state'] = 'alive'
        else:
            if isinstance(sup.desc.proc, K8sProcess): # hacky way to check the pm
                exit_code = sup.desc.proc.status()
            else:
                try:
                    exit_code = sup.desc.proc.exit_code
                except sh.ErrorReturnCode as e:
                    exit_code = e.exit_code
            ret['process_state'] = f'dead[{exit_code}]'
        ret['ping'] = sup.commander.ping()
        ret['last_cmd_failed'] = (sup.last_sent_command != sup.last_ok_command)
        ret['name'] = node.name
        ret['state'] = ("error " if node.errored else "") + node.state + ("" if node.included else " - excluded")
        ret['host'] = sup.desc.node if hasattr(sup.desc, 'node') else sup.desc.host,
        ret['last_sent_command'] = sup.last_sent_command
        ret['last_ok_command'] = sup.last_ok_command
    else:
        ret['name'] = node.name
        ret['state'] = ("error " if node.errored else "") + node.state
        if get_children:
            ret['children'] = [status_data(child) for child in node.children]
    return ret


def print_status(topnode, console, apparatus_id='', partition='', conf='') -> int:
    table = Table(title=f"[bold]{apparatus_id}[/bold] applications" + (f" in partition [bold]{partition}[/bold]" + (f" running using [bold]{conf}[/bold] configuration" if partition else '')))
    table.add_column("name", style="blue")
    table.add_column("state", style="blue")
    table.add_column("host", style="magenta")
    table.add_column("pings", style="magenta")
    table.add_column("last cmd")
    table.add_column("last succ. cmd", style="green")

    for pre, _, node in RenderTree(topnode):
        if isinstance(node, ApplicationNode):
            sup = node.sup

            if sup.desc.proc.is_alive():
                alive = 'alive'
            else:
                proc = sup.desc.proc
                exit_code = None
                if isinstance(proc, K8sProcess): # hacky way to check the pm
                    exit_code = sup.desc.proc.status()
                else:
                    try:
                        exit_code = sup.desc.proc.exit_code
                    except sh.ErrorReturnCode as e:
                        exit_code = e.exit_code

                alive = f'dead[{exit_code}]'

            ping = sup.commander.ping()
            last_cmd_failed = (sup.last_sent_command != sup.last_ok_command)

            state_str = ''
            style = ''
            if node.errored:
                state_str += "ERROR - "
                style = 'bold red'
            state_str += f"{node.state} - {alive}"
            if not node.included:
                state_str += " - excluded"
                style = 'bright_black' # bright_black?

            state_txt = Text(state_str, style=(style))

            table.add_row(
                Text(pre)+Text(node.name),
                state_txt,
                sup.desc.node if hasattr(sup.desc, 'node') else sup.desc.host,
                str(ping),
                Text(str(sup.last_sent_command), style=('bold red' if last_cmd_failed else '')),
                str(sup.last_ok_command)
            )

        else:
            state_str = ''
            style = ''
            if node.errored:
                state_str += "ERROR - "
                style = 'bold red'
            state_str += f"{node.state}"
            if not node.included:
                state_str += " - excluded"
                style = 'bright_black'

            state_txt = Text(state_str, style=(style))

            table.add_row(
                Text(pre)+Text(node.name),
                state_txt
            )

    console.print(table)

def print_node(node, console, leg:bool=False) -> int:
    rows = []
    try:
        for pre, _, all_node in RenderTree(node):
            if all_node == node:
                rows.append(f"{pre}[red]{all_node.name}[/red]")
            elif isinstance(all_node, SubsystemNode):
                rows.append(f"{pre}[yellow]{all_node.name}[/yellow]")
            elif isinstance(all_node, ApplicationNode):
                rows.append(f"{pre}[blue]{all_node.name}[/blue]")
            else:
                rows.append(f"{pre}{all_node.name}")

        console.print(Panel.fit('\n'.join(rows)))

        if leg:
            console.print("\nLegend:")
            console.print(" - [red]top node[/red]")
            console.print(" - [yellow]subsystems[/yellow]")
            console.print(" - [blue]applications[/blue]\n")

    except Exception as ex:
        console.print("Tree is corrupted!")
        return_code = 14
        raise RuntimeError("Tree is corrupted")
    return 0
