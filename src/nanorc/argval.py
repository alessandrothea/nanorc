from anytree.resolver import Resolver
from urllib.parse import urlparse, ParseResult
from os import path
import click

def validate_path_exists(prompted_path):
    if not prompted_path: return prompted_path
    if not path.exists(prompted_path):
        raise RuntimeError(f"Couldn't find {prompted_path} in filesystem")
    return prompted_path


def validate_timeout(ctx, param, timeout):
    if timeout is None:
        return timeout
    if timeout<=0:
        raise click.BadParameter('Timeout should be >0')
    return timeout

def validate_stop_wait(ctx, param, stop_wait):
    if stop_wait is None:
        return stop_wait
    if stop_wait<0:
        raise click.BadParameter('Stop wait should be >=0')
    return stop_wait

def validate_node_path(ctx, param, prompted_path):

    if prompted_path is None:
        return None

    if prompted_path[0] != '/':
        prompted_path = '/'+prompted_path

    hierarchy = prompted_path.split("/")
    topnode = ctx.obj.rc.topnode

    r = Resolver('name')
    try:
        node = r.get(topnode, prompted_path)
        return node
    except Exception as ex:
        raise click.BadParameter(f"Couldn't find {prompted_path} in the tree") from ex

    return node

def validate_partition_number(ctx, param, number):
    if number<0 or number>10:
        raise click.BadParameter(f"Partition number should be between 0 and 10 (you fed {number})")
    return number


def validate_conf(ctx, param, top_cfg):
    confurl = urlparse(top_cfg)
    if path.isdir(confurl.path):
        confurl=ParseResult(
            scheme='dir',
            path=top_cfg,
            netloc='', params='', query='', fragment='')
        return confurl
    if path.exists(confurl.path) and confurl.path[-5:]=='.json':
        confurl=ParseResult(
            scheme='file',
            path=top_cfg,
            netloc='', params='', query='', fragment='')
        return confurl
    if confurl.scheme == 'confservice':
        return confurl

    raise click.BadParameter(f"TOP_CFG should either be a directory, a json file, or a config service utility with the form confservice://the_conf_name?1 (where the ?1 at the end is optionnal and represents the version). You provided: '{top_cfg}'")