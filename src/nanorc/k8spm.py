#!/usr/bin/env python

import logging
import rich
import socket
import time
import json
import copy as cp
import os
from urllib.parse import urlparse
from kubernetes import client, config
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn, TimeElapsedColumn, track
from rich.table import Table

from datetime import datetime

class AppProcessDescriptor(object):
    """docstring for AppProcessDescriptor"""

    def __init__(self, name):
        super(AppProcessDescriptor, self).__init__()
        self.name = name
        self.host = None
        self.node = None
        self.conf = None
        self.port = None
        self.proc = None


    def __str__(self):
        return str(vars(self))

class K8sProcess(object):

    def __init__(self, pm, name, namespace):
        self.pm = pm
        self.name = name
        self.namespace = namespace

    def is_alive(self):
        try:
            s = self.pm._core_v1_api.read_namespaced_pod_status(self.name, self.namespace)
            for cond in s.status.conditions:
                if cond.type == "Ready" and cond.status == "True":
                    return True
            return False
        except:
            return False

    def status(self):
        try:
            s = self.pm._core_v1_api.read_namespaced_pod_status(self.name, self.namespace)
            container_status = s.status.container_statuses[0].state
            if   container_status.running:
                return "Running"
            elif container_status.terminated:
                return f"Terminated {container_status.terminated.exit_code} {container_status.terminated.reason}"
            elif container_status.waiting:
                return f"Waiting {container_status.waiting.reason}"
            else:
                return 'Unknown'
        except:
            return 'Unknown'



class K8SProcessManager(object):
    def __init__(self, console: Console, cluster_config, connections, log_path=None):
        """A Kubernetes Process Manager

        Args:
            console (Console): Description
        """
        super(K8SProcessManager, self).__init__()
        self.log = logging.getLogger(__name__)
        self.log_path = log_path
        self.connections = connections
        self.mount_cvmfs = True
        self.console = console
        self.apps = {}
        self.partition = None
        self.cluster_config = cluster_config

        config.load_kube_config()

        self._core_v1_api = client.CoreV1Api()
        self._apps_v1_api = client.AppsV1Api()


    def execute_script(self, script_data):
        ## Pin thread file can be anywhere in the filesystem
        ## so below won't work.

        # from kubernetes.stream import stream

        # env_vars = script_data["env"]
        # cmd =';'.join([ f"export {n}=\"{v}\"" for n,v in env_vars.items()])
        # cmd += ";"+"; ".join(script_data['cmd'])
        # pods = self.list_pods(self.partition)
        # for pod in pods.items:
        #     resp = stream(
        #         self._core_v1_api.connect_get_namespaced_pod_exec, pod.metadata.name, self.partition,
        #         command=cmd,
        #         stderr=True, stdin=False,
        #         stdout=True, tty=False
        #     )
        #     # ssh_args = [host, "-tt", "-o StrictHostKeyChecking=no"] + [cmd]
        #     # proc = sh.ssh(ssh_args)
        #     self.log.info(resp)

        ## Instead we revert to ssh...
        env_vars = script_data["env"]
        cmd = ''
        pretty_print = ''
        for n,v in env_vars.items():
            cmd += f"export {n}=\"{v}\"; "
            pretty_v = v
            if len(v)>100:
                pretty_v = v[:50]+'...'+v[-50:]
            pretty_print += f"export {n}=\"{pretty_v}\"; "

        cmd += "; ".join(script_data['cmd'])
        pretty_print += "; ".join(script_data['cmd'])

        pods = self.list_pods(self.partition)
        hosts = set([self.get_pod_node(pod.metadata.name, self.partition) for pod in pods.items])

        for host in hosts:
            self.console.print(f'Executing {script_data["cmd"]} script on \'{host}\':\n[bright_black]{pretty_print}[/]')
            ssh_args = [host, "-tt", "-o StrictHostKeyChecking=no"] + [cmd]
            import sh
            from sh import ErrorReturnCode
            try:
                import os
                host_env = os.environ
                ssh_env = {'KRB5CCNAME': host_env['KRB5CCNAME'] } if 'KRB5CCNAME' in host_env else {}
                ssh_cmd  = sh.Command('/usr/bin/ssh')
                proc = ssh_cmd(ssh_args, _env=ssh_env)
            except ErrorReturnCode as e:
                self.log.error(
                    e.stdout.decode('ascii')
                )
                continue
            except Exception as e:
                self.log.critical(
                    str(e)
                )
            self.log.debug(proc)



    def list_pods(self, namespace):
        ret = self._core_v1_api.list_namespaced_pod(namespace)
        # for i in ret.items:
        # self.log.info("%s\t%s\t%s" % (i.status.pod_ip, i.metadata.namespace, i.metadata.name))
        return ret

    def list_endpoints(self):
        self.log.info("Listing endpoints:")
        ret = self._core_v1_api.list_endpoints_for_all_namespaces(watch=False)
        for i in ret.items:
            self.log.info("%s\t%s" % (i.metadata.namespace, i.metadata.name))
        return ret

    # ----
    def create_namespace(self, namespace : str):
        nslist = [ns.metadata.name for ns in self._core_v1_api.list_namespace().items]
        if namespace in nslist:
            self.log.debug(f"Not creating \"{namespace}\" namespace as it already exist")
            self.log.info(f"Try `kubectl get pods -n {namespace}' and see if there is anything running")
            self.log.info(f"If you are sure nothing is running and want to use same session name `kubectl delete ns {namespace}' before starting your next run")
            return

        self.log.info(f"Creating \"{namespace}\" namespace")
        ns = client.V1Namespace(
            metadata=client.V1ObjectMeta(name=namespace)
        )
        try:
            resp = self._core_v1_api.create_namespace(
                body=ns
            )
        except Exception as e:
            self.log.error(e)
            raise RuntimeError(f"Failed to create namespace \"{namespace}\"") from e


        metadata = {
            "metadata": {
                "labels": {
                    "pod-security.kubernetes.io/enforce":"privileged",
                    "pod-security.kubernetes.io/enforce-version":"latest",
                    "pod-security.kubernetes.io/warn":"privileged",
                    "pod-security.kubernetes.io/warn-version":"latest",
                    "pod-security.kubernetes.io/audit":"privileged",
                    "pod-security.kubernetes.io/audit-version":"latest"
                }
            }
        }

        self._core_v1_api.patch_namespace(namespace, metadata)

    # ----
    def delete_namespace(self, namespace: str):
        nslist = [ns.metadata.name for ns in self._core_v1_api.list_namespace().items]
        if not namespace in nslist:
            self.log.info(f"The \"{namespace}\" namespace has already been deleted")
            return
        self.log.info(f"Deleting \"{namespace}\" namespace")
        try:
            #
            resp = self._core_v1_api.delete_namespace(
                name=namespace
            )
        except Exception as e:
            self.log.error(e)
            raise RuntimeError(f"Failed to delete namespace \"{namespace}\"") from e

    def get_pod_node(self, pod_name, partition):
        pod_list = self._core_v1_api.list_namespaced_pod(partition)
        for pod in pod_list.items:
            if pod.metadata.name == pod_name:
                return pod.spec.node_name
        return 'unknown'

    def get_container_port_list_from_connections(self, app_name:str, connections:list=None, cmd_port:int=3333):
        ret = [
            client.V1ContainerPort(
                name = 'restcmd',
                protocol = "TCP",
                container_port = cmd_port,
            )]

        for c in connections:
            uri = urlparse(c['uri'])
            if uri.hostname != app_name: continue
            name = c['id']['uid'].replace(".", "").replace("_", "").replace("$","").replace("{", "").replace("}", "").lower()[-15:]
            self.log.debug(f'Opening port {uri.port} (named {name}) in {app_name}\'s container')

            ret += [
                client.V1ContainerPort(
                    name = name,
                    protocol = uri.scheme.upper(),
                    container_port = uri.port,
                )]
        return ret


    def get_service_port_list_from_connections(self, app_name:str, connections:list=None, cmd_port:int=3333):
        ret = [
            client.V1ServicePort(
                name = 'restcmd',
                protocol = "TCP",
                target_port = cmd_port,
                port = cmd_port,
            )]

        for c in connections:
            uri = urlparse(c['uri'])
            if uri.hostname != app_name: continue
            name = c['id']['uid'].replace(".", "").replace("_", "").replace("$","").replace("{", "").replace("}", "").lower()[-15:]
            self.log.debug(f'Creating service {uri.port} (named {name}) in {app_name}\'s container')

            ret += [
                client.V1ServicePort(
                    name = name,
                    protocol = uri.scheme.upper(),
                    target_port = uri.port,
                    port = uri.port,
                )]
        return ret

    def get_node_affinity(self, info:dict):
        if not info: return None
        # node_selection
        node_selector_terms_required = []
        scheduled_terms_preferred = []

        first_preferred_weight = 100

        for node_affinity in info:
            strict = node_affinity['strict']
            del node_affinity['strict']

            if strict:
                node_selector_terms_required += [
                    client.V1NodeSelectorTerm(
                        match_expressions = [
                            {
                                'key': key,
                                "operator": 'In',
                                'values': values,
                            }
                            for key, values in node_affinity.items()
                        ]
                    )
                ]

            else:
                scheduled_terms_preferred += [
                    client.V1PreferredSchedulingTerm(
                        weight = first_preferred_weight,
                        preference = client.V1NodeSelectorTerm(
                            match_expressions = [
                                {
                                    'key': key,
                                    "operator": 'In',
                                    'values': values,
                                }
                            for key, values in node_affinity.items()
                            ]
                        )
                    )
                ]
                first_preferred_weight -= 1

        return client.V1NodeAffinity(
            required_during_scheduling_ignored_during_execution = client.V1NodeSelector(
                node_selector_terms = node_selector_terms_required
            ) if node_selector_terms_required else None,
            preferred_during_scheduling_ignored_during_execution = scheduled_terms_preferred
        )


    def get_pod_affinity(self, affinities:list, affinity_sign:bool=True): #affnity_sign=false for anti-affinity
        if not affinities: return None
        # affinity
        pod_affinity_terms_required = []
        pod_affinity_terms_preferred = []

        first_preferred_weight = 100

        for affinity in affinities:
            strict = affinity['strict']
            del affinity['strict']

            if strict:
                pod_affinity_terms_required += [
                    client.V1PodAffinityTerm(
                        topology_key="kubernetes.io/hostname",#??? not sure what this does
                        label_selector=client.V1LabelSelector(
                            match_expressions=[
                                client.V1LabelSelectorRequirement(
                                    key=key,
                                    operator="In",
                                    values=values,
                                )
                                for key, values in affinity.items()
                            ]
                        )
                    )
                ]
            else:
                pod_affinity_terms_preferred += [
                    client.V1WeightedPodAffinityTerm(
                        weight = first_preferred_weight,
                        pod_affinity_term = client.V1PodAffinityTerm(
                            topology_key="kubernetes.io/hostname",#??? not sure what this does
                            label_selector=client.V1LabelSelector(
                                match_expressions=[
                                    client.V1LabelSelectorRequirement(
                                        key=key,
                                        operator="In",
                                        values=values,
                                    )
                                for key, values in affinity.items()
                                ]
                            )
                        )
                    )
                ]
                first_preferred_weight -= 1

        if affinity_sign:
            return client.V1PodAffinity(
                required_during_scheduling_ignored_during_execution=pod_affinity_terms_required,
                preferred_during_scheduling_ignored_during_execution=pod_affinity_terms_preferred
            )
        else:
            return client.V1PodAntiAffinity(
                required_during_scheduling_ignored_during_execution=pod_affinity_terms_required,
                preferred_during_scheduling_ignored_during_execution=pod_affinity_terms_preferred
            )

    # ----
    def create_daqapp_pod(
            self,
            name: str,
            app_label: str,
            app_boot_info:dict,
            namespace: str,
            run_as: dict = None):

        info_str  = f"Creating \"{namespace}:{name}\" DAQ App"
        debug_str = f"image: \"{app_boot_info['image']}\""
        if app_boot_info['resources']:
            debug_str += f' resources: {app_boot_info["resources"]}'
        if app_boot_info['mounted_dirs']:
            debug_str+=f' mounted_dirs (name: inpod->physical)={mount["name"]+": "+mount["in_pod_location"]+"->"+mount["physical_location"] for mount in app_boot_info["mounted_dirs"]}'

        if app_boot_info['node-selection']:
            debug_str+=f' node-selection={app_boot_info["node-selection"]}'
        if app_boot_info['affinity']:
            debug_str+=f' affinity={app_boot_info["affinity"]}'
        if app_boot_info['anti-affinity']:
            debug_str+=f' anti-affinity={app_boot_info["anti-affinity"]}'

        self.log.info(info_str)
        self.log.debug(debug_str)

        ## Need to mount /dev and be privileged in this case...

        pod = client.V1Pod(
            # Run the pod with same user id and group id as the current user
            # Required in kind environment to create non-root files in shared folders
            metadata = client.V1ObjectMeta(
                name=name,
                labels={"app": app_label}
            ),
            spec = client.V1PodSpec(
                restart_policy = "Never",
                security_context = client.V1PodSecurityContext(
                    run_as_user = run_as['uid'],
                    run_as_group = run_as['gid'],
                ) if run_as else None,
                host_pid = True, # HACK
                affinity = client.V1Affinity(
                    node_affinity = self.get_node_affinity(app_boot_info['node-selection']),
                    pod_affinity      = self.get_pod_affinity(app_boot_info['affinity']     , affinity_sign=True ),
                    pod_anti_affinity = self.get_pod_affinity(app_boot_info['anti-affinity'], affinity_sign=False),
                ),
                # List of processes
                containers = [
                    # DAQ application container
                    client.V1Container(
                        name = "daq-application",
                        image = app_boot_info["image"],
                        image_pull_policy= "Always",
                        security_context = client.V1SecurityContext(
                            privileged = app_boot_info['privileged'],
                            capabilities = client.V1Capabilities(
                                add = app_boot_info['capabilities']
                            )
                        ),
                        resources = (
                            client.V1ResourceRequirements(
                                app_boot_info['resources']
                            )
                        ),
                        # Environment variables
                        env = [
                            client.V1EnvVar(
                                name=k,
                                value=str(v)
                            ) for k,v in app_boot_info['env'].items()
                        ],
                        command=app_boot_info['command'],
                        args=app_boot_info['args'],
                        ports=self.get_container_port_list_from_connections(
                            app_name=name,
                            connections=app_boot_info['connections'],
                            cmd_port=app_boot_info['cmd_port']
                        ),
                        volume_mounts=(
                            (
                                [
                                    client.V1VolumeMount(
                                        mount_path = mount['in_pod_location'],
                                        name = mount['name'],
                                        read_only = mount['read_only'])
                                    for mount in app_boot_info['mounted_dirs']
                                ]
                            )
                        )
                    )
                ],
                volumes=(
                    (
                        [
                            client.V1Volume(
                                name = mount['name'],
                                host_path = client.V1HostPathVolumeSource(
                                    path = mount['physical_location']))
                            for mount in app_boot_info['mounted_dirs']
                        ]
                    )
                )
            )
        )

        self.log.debug(pod)

        # Creation of the pod in specified namespace
        try:
            #
            resp = self._core_v1_api.create_namespaced_pod (
                namespace = namespace,
                body = pod
            )
        except Exception as e:
            self.log.error(e)
            raise RuntimeError(f"Failed to create daqapp pod \"{namespace}:{name}\"") from e

        service = client.V1Service(
            metadata = client.V1ObjectMeta(name=name),
            spec = client.V1ServiceSpec(
                ports = self.get_service_port_list_from_connections(app_name=name, connections=app_boot_info['connections'], cmd_port=app_boot_info['cmd_port']),
                selector = {"app": app_label}
            )
        )  # V1Service
        #self.log.debug(service)

        try:
            resp = self._core_v1_api.create_namespaced_service(namespace, service)
        except Exception as e:
            self.log.error(e)
            raise RuntimeError(f"Failed to create daqapp service \"{namespace}:{name}\"") from e

    # ----
    def create_egress_endpoint(self, name: str, namespace: str, ip: str, port: int):

        self.log.info(f"Creating egress service \"{namespace}:{name}\" for \"{ip}:{port}\"")
        # Creating Service object
        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=name,
            ),
            spec=client.V1ServiceSpec(
                ports=[
                    client.V1ServicePort(
                        protocol = 'TCP',
                        target_port = port,
                        port = port,
                    )
                ],
            )
        )  # V1Service

        try:
            resp = self._core_v1_api.create_namespaced_service(namespace, service)
        except Exception as e:
            self.log.error(e)
            raise RuntimeError(f"Failed to create nanorc responder service \"{namespace}:{name}\"") from e

        self.log.info(f"Creating egress responder endpoint {ip}:{port}")

        # Create Endpoints Objects
        endpoints = client.V1Endpoints(
            metadata = client.V1ObjectMeta(name=name),
            subsets=[
                client.V1EndpointSubset(
                    addresses = [
                        client.V1EndpointAddress(ip=ip)
                    ],
                    ports=[
                        client.CoreV1EndpointPort(port=port)
                    ]
                )
            ]
        )
        #self.log.debug(endpoints)

        try:
            self._core_v1_api.create_namespaced_endpoints(namespace, endpoints)
        except Exception as e:
            self.log.error(e)
            raise RuntimeError(f"Failed to create nanorc responder endpoint \"{namespace}:{name}\"") from e

    """
    ---
    # Source: cvmfs-csi/templates/persistentvolumeclaim.yaml
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: dunedaq.opensciencegrid.org
    spec:
      accessModes:
      - ReadOnlyMany
      resources:
        requests:
          storage: 1Gi
      storageClassName: dunedaq.opensciencegrid.org
    """
    def create_cvmfs_pvc(self, name: str, namespace: str):

        # Create claim
        claim = client.V1PersistentVolumeClaim(
            # Meta-data
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace
            ),
            # Claim
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=['ReadOnlyMany'],
                resources=client.V1ResourceRequirements(
                        requests={'storage': '2Gi'}
                    ),
                storage_class_name=name
                )
            )

        try:
            self._core_v1_api.create_namespaced_persistent_volume_claim(namespace, claim)
        except Exception as e:
            self.log.error(e)
            raise RuntimeError(f"Failed to create persistent volume claim \"{namespace}:{name}\"") from e


    def create_data_pvc(self, pvc:dict, namespace:str):
        # Create claim
        claim = client.V1PersistentVolumeClaim(
            # Meta-data
            metadata=client.V1ObjectMeta(
                name=pvc['claim_name'],
                namespace=namespace
            ),
            # Claim
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=['ReadWriteOnce'], # ONCE? Does't work with ReadOnlyMany, but that doesn't seem to break things...
                storage_class_name=pvc['storage_class_name'],
                resources=client.V1ResourceRequirements(
                    requests={'storage': '2Gi'}
                ),
            )
        )

        try:
            self._core_v1_api.create_namespaced_persistent_volume_claim(namespace, claim)
        except Exception as e:
            self.log.error(e)
            raise RuntimeError(f"Failed to create persistent volume claim \"{namespace}:{name}\"") from e


    def add_mounted_dir(self, in_pod_location, physical_location, name, read_only=True):
        forbidden_paths = ['/','/boot','/etc','/lib','/proc','/sys','/usr','/tmp']

        import os

        physical_loc_apath = os.path.abspath(physical_location)

        if physical_loc_apath in forbidden_paths:
            raise RuntimeError(f'You are not allowed to mount \'{physical_location}\', required for \'{name}\'')

        in_pod_location_apath = os.path.abspath(in_pod_location)

        if in_pod_location_apath in forbidden_paths:
            raise RuntimeError(f'You are not allowed to mount \'{in_pod_location_apath}\', required for \'{name}\'')

        return {
            'in_pod_location': in_pod_location,
            'physical_location': physical_location,
            'name': name,
            'read_only': read_only,
        }


    #---
    def boot(self, boot_info, timeout, conf_loc, **kwargs):

        if self.apps:
            raise RuntimeError(
                f"ERROR: apps have already been booted {' '.join(self.apps.keys())}. Terminate them all before booting a new set."
            )



        #NOTE:  Move this out of the k8s pm-----
        if self.cluster_config.is_kind:
            logging.info('Resolving the kind gateway')
            import docker, ipaddress
            # Detect docker environment
            docker_client = docker.from_env()

            # Find the docker network called Kind
            try:
                kind_network = next(iter(n for n in docker_client.networks.list() if n.name == 'kind'))
            except Exception as exc:
                raise RuntimeError("Failed to identfy docker network 'kind'") from exc

            # And extract the gateway ip, which corresponds to the host
            try:
                self.gateway = next(iter(s['Gateway'] for s in kind_network.attrs['IPAM']['Config'] if isinstance(ipaddress.ip_address(s['Gateway']), ipaddress.IPv4Address)), None)
            except Exception as exc:
                raise RuntimeError("Identify the kind gateway address'") from exc
            logging.info(f"Kind network gateway: {self.gateway}")
        else:
            self.gateway = socket.gethostbyname(socket.gethostname())
            logging.info(f"K8s gateway: {self.gateway} ({socket.gethostname()})")
        #---------------------------

        apps = boot_info["apps"].copy()
        env_vars = boot_info["env"]
        rte_script = boot_info.get('rte_script')

        mounted_dirs = []

        #NOTE:  Move this out of the k8s pm-----
        if rte_script:
            self.log.info(f'Using the Runtime environment script "{rte_script}"')
        else:
            from nanorc.utils import get_rte_script
            rte_script = get_rte_script()

        from nanorc.utils import release_or_dev

        if release_or_dev() == "dev":
            dbt_install_dir = os.getenv('DBT_INSTALL_DIR')
            mounted_dirs += [self.add_mounted_dir(
                in_pod_location = dbt_install_dir,
                name = 'installdir',
                read_only = True,
                physical_location = dbt_install_dir
            )]
            self.log.info(f'Using the dev area "{dbt_install_dir}"')

            # Check this
            venv_dir = os.getenv('VIRTUAL_ENV')

            mounted_dirs += [self.add_mounted_dir(
                in_pod_location = venv_dir,
                name = 'venv',
                read_only = True,
                physical_location = venv_dir
            )]

        #--------------

        self.partition = boot_info['env']['DUNEDAQ_PARTITION']

        # Create partition
        self.create_namespace(self.partition)

        run_as = {
            'uid': os.getuid(),
            'gid': os.getgid(),
        }

        log_dir = self.log_path if self.log_path else f'{os.getcwd()}/logs'
        if not os.path.exists(log_dir):
            os.mkdir(log_dir)

        mounted_dirs += [self.add_mounted_dir(
            in_pod_location = '/logs',
            name = 'logdir',
            read_only = False,
            physical_location = log_dir
        )]

        for app_name in boot_info['order']:
            app_conf = apps[app_name]
            cmd_port = app_conf['port']
            env_formatter = {
                "DUNEDAQ_PARTITION": env_vars['DUNEDAQ_PARTITION'],
                "APP_NAME": app_name,
                "APP_HOST": app_name, # For the benefit of TRACE_FILE... Hacky...
                "APP_PORT": cmd_port,
                "CONF_LOC": conf_loc,
            }

            exec_data = boot_info['exec'][app_conf['exec']]
            exec_vars_cp = cp.deepcopy(exec_data['env'])
            exec_vars = {}

            for k,v in exec_vars_cp.items():
                exec_vars[k]=v.format(**env_formatter) if type(v) is str else v

            app_env = {}
            app_env.update(env_vars)
            app_env.update(exec_vars)

            env_formatter.update(app_env)
            app_args = [a.format(**env_formatter) for a in boot_info['exec'][app_conf['exec']]['args']]
            app_img = exec_data['image']
            app_cmd = exec_data['cmd']
            if not app_img:
                raise RuntimeError("You need to specify an image in the configuration!")

            app_boot_info = {
                "env"             : {},
                "command"         : app_cmd,
                "args"            : app_args,
                "image"           : app_img,
                "cmd_port"        : cmd_port,
                "mounted_dirs"    : app_conf.get('mounted_dirs', [])+ mounted_dirs,
                "resources"       : app_conf.get('resources',  {}),
                "affinity"        : app_conf.get('affinity', []),
                "anti-affinity"   : app_conf.get('anti-affinity', []),
                "node-selection"  : app_conf.get('node-selection', []),
                "connections"     : self.connections.get(app_name, []),
                "privileged"      : app_conf.get('privileged', False),
                "capabilities"    : app_conf.get('capabilities', []),
            }

            trace = app_env.get('TRACE_FILE')

            if trace:
                trace_dir = f'/tmp/trace-buffers/{env_vars["DUNEDAQ_PARTITION"]}/{app_name}'
                if not os.path.exists(trace_dir):
                    os.makedirs(trace_dir)

                trace_uri = urlparse(trace)
                tpath = os.path.dirname(trace_uri.path)#+'trace'
                #tfile = os.path.basename(trace_uri.path)

                app_boot_info['mounted_dirs'] += [self.add_mounted_dir(
                    in_pod_location = tpath,
                    name = 'tracedir',
                    read_only = False,
                    physical_location = trace_dir
                )]
                #app_env['TRACE_FILE'] = f'{tpath}/{tfile}'

            if self.mount_cvmfs:
                app_boot_info['mounted_dirs'] += [
                    self.add_mounted_dir(
                        in_pod_location = '/cvmfs/dunedaq.opensciencegrid.org',
                        name = 'dunedaq-cvmfs',
                        read_only = True,
                        physical_location = '/cvmfs/dunedaq.opensciencegrid.org'
                    ),
                    self.add_mounted_dir(
                        in_pod_location = '/cvmfs/dunedaq-development.opensciencegrid.org',
                        name = 'dunedaq-dev-cvmfs',
                        read_only = True,
                        physical_location = '/cvmfs/dunedaq-development.opensciencegrid.org'
                    )
                ]

            if self.cluster_config.is_kind:
                app_boot_info['mounted_dirs'] += [self.add_mounted_dir(
                    in_pod_location = '/dunedaq/pocket',
                    name = 'pocket',
                    read_only = False,
                    physical_location = '/pocket'
                )]


            now = datetime.now() # current date and time
            date_time = now.strftime("%Y-%m-%d_%H%M%S")
            log_file = f'log_{date_time}_{app_name}_{app_conf["port"]}.txt'

            from nanorc.utils import strip_env_for_rte
            app_boot_info["env"] = strip_env_for_rte(app_env)
            app_boot_info['command'] = ['/bin/bash', '-c']
            app_boot_info['args'] = [f'{{ source {rte_script} && {app_cmd} {" ".join(app_args)} ; }} | tee /logs/{log_file}']


            if self.cluster_config.is_kind:
                # discard most of the nice features of k8s if we use kind
                app_boot_info["node-selection"] = None
                app_boot_info["affinity"] = None
                app_boot_info["anti-affinity"] = None

            self.log.debug(json.dumps(app_boot_info, indent=2))
            app_desc = AppProcessDescriptor(app_name)
            app_desc.conf = app_conf.copy()
            app_desc.partition = self.partition
            app_desc.host = f'{app_name}.{self.partition}'
            app_desc.pod = ''
            app_desc.port = cmd_port
            app_desc.proc = K8sProcess(self, app_name, self.partition)

            k8s_name = app_name#.replace("_", "-").replace(".", "")

            self.create_daqapp_pod(
                name = k8s_name, # better kwargs all this...
                app_label = k8s_name,
                app_boot_info = app_boot_info,
                namespace = self.partition,
                run_as = run_as
            )

            app_desc.node = self.get_pod_node(k8s_name, self.partition)

            self.apps[app_name] = app_desc

        def rdm_string(N:int=5):
            import string
            import random
            return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(N))

        responder_name = f'nanorc-{rdm_string()}'
        self.create_egress_endpoint(
            name = responder_name,
            namespace = self.partition,
            ip = self.gateway,
            port = boot_info["response_listener"]["port"])
        self.nanorc_responder = responder_name


        if 'external_services' in boot_info:
            for name, svc in boot_info['external_services'].items():
                info_ip = socket.gethostbyname(svc['host'])
                self.create_egress_endpoint(
                    name = name,
                    namespace = self.partition,
                    ip = info_ip,
                    port = svc['port']
                )


        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            TimeElapsedColumn(),
            console=self.console,
        ) as progress:
            total = progress.add_task("[yellow]# apps started", total=len(self.apps))
            apps_tasks = {
                a: progress.add_task(f"[blue]{a}", total=1) for a in self.apps
            }
            waiting = progress.add_task("[yellow]timeout", total=timeout)

            for _ in range(timeout):
                progress.update(waiting, advance=1)

                ready = self.check_apps()
                for a, t in apps_tasks.items():
                    if a in ready:
                        progress.update(t, completed=1)
                        self.apps[a].pod = ready[a]
                progress.update(total, completed=len(ready))
                r = list(ready.keys())
                a = list(self.apps.keys())
                r.sort()
                a.sort()
                if r==a:
                    progress.update(waiting, visible=False)
                    break

                time.sleep(1)



    # ---
    def check_apps(self):
        ready = {}
        for p in self._core_v1_api.list_namespaced_pod(self.partition).items:
            for name in self.apps.keys():
                if name in p.metadata.name and p.status.phase == "Running":
                    ready[name]=p.metadata.name
        return ready

    # ---
    def terminate(self):

        timeout = 60
        if self.partition:
            self.delete_namespace(self.partition)

            for _ in track(range(timeout), description="Terminating namespace..."):

                s = self._core_v1_api.list_namespace()
                found = False
                for namespace in s.items:
                    if namespace.metadata.name == self.partition:
                        found = True
                        break
                if not found:
                    return
                time.sleep(1)

            logging.warning('Timeout expired!')


# ---
def main():

    from rich.logging import RichHandler

    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)]
    )

    partition = 'dunedaq-0'
    pm = K8SProcessManager()
    # pm.list_pods()
    # pm.list_endpoints()
    pm.create_namespace(partition)
    pm.create_cvmfs_pvc('dunedaq.opensciencegrid.org', partition)
    pm.create_daqapp_pod('trigger', 'trg', partition, True)
    pm.create_egress_endpoint('nanorc', 'nanorc', partition, '128.141.174.0', 56789)
    # pm.list_pods()

if __name__ == '__main__':
    main()
