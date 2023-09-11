import json
import os
import pytest
import subprocess
import tempfile

app_name = "trigger"

expert_json = {"id": "record", "entry_state": "ANY", "exit_state": "ANY", "data": {}}
conf_types = ["normal"]
commands = "boot expert_command expert.json test-conf/test-conf/dfo".split()
conf_name = "test-conf"
cluster_address = "k8s://np04-srv-015:31000"

@pytest.fixture(params = conf_types)
def perform_all_runs(request):
    '''
    We generate a config using daqconf_multiru_gen, then run nanorc with it in two different ways.
    The error code of the process is used to determine whether everything worked.
    All processes are run in a temporary directory, so as not to fill up the CWD with logs.
    '''
    start_dir = os.getcwd()
    temp_dir_object = tempfile.TemporaryDirectory()
    temp_dir_name = temp_dir_object.name                                        #Make a temp directory.
    os.popen(f'cp {start_dir}/my_dro_map.json {temp_dir_name}/my_dro_map.json') #Copy the DRO map inside.
    os.chdir(temp_dir_name)                                                     #Move into the temp dir.

    DMG_args = ["daqconf_multiru_gen", "-m", "my_dro_map.json", conf_name]
    subprocess.run(DMG_args)                                                    #Generate a config
    partition_name = f"test-partition-{request.param}"
    with open('expert.json', 'w') as json_file1:
        json.dump(expert_json, json_file1)

    match request.param:
        case "normal":
            arglist = ["nanorc", conf_name, partition_name] + commands

        case "k8s":
            arglist = ["nanorc", "--pm", cluster_address, conf_name, partition_name] + commands

    output = subprocess.run(arglist)
    os.chdir(start_dir)
    return output.returncode

def test_no_errors(perform_all_runs):
    assert perform_all_runs == 0

