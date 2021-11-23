#!/bin/python3

import os, subprocess, yaml, time, socket, datetime, getopt, sys, re, atexit, logging as log
from pathlib import Path

help_text = ''

def main(args):

    # arg-settings
    arg_short_options = "hc:fv"
    arg_long_options  = ["help", "config=", "cmd=", "force", "verbose"]

    global help_text
    help_text = f"""
    Usage:
    {args[0]} [options]\n
    Options:
      -h --help         Display this text
      -c --config       Specify the config-file                 default='backup.conf'
      --cmd             Run command for repository
      -f --force        Force backup (ignore all conditions)"""


    args = readArgs(
        args          = args[1:],
        short_options = arg_short_options,
        long_options  = arg_long_options
        )

    args = parseCliArgs(
        args    = args,
        data    = {
            'config':   "backup.conf",
            'force':    False,
            'verbose':  False
        },
        argMap  = {
            'config':  ['config',  'c'],
            'cmd':     ['cmd'],
            'force':   ['force',   'f'],
            "verbose": ['verbose', 'v']
        }
        )
    
    setupLogging(args['verbose'])

    config = readConfig(args['config'])
    setDefaultValues(config, args['config'])

    # Execute cmd if requested
    if "cmd" in args:
        return executeBorgCommand(config, args['cmd'])

    # Get config data
    include = config["backup"]["include"]
    exclude = config["backup"]["exclude"]

    
    if not args['force']:

        # Pre-backup-checks
        result = checkRunConditions(config)
        if not (isinstance(result, bool) and result):
            log.error(result)
            return 0

        # Previous-backup-metadata
        metadata = readBackupMetadata(config)
        if metadata:
            handleBackupMetadata(config, metadata)

    # Folder-scan
    if config["backup"]["scan"]["enabled"]:
        includeScanned, excludeScanned = scanForFolders(config)

        # Touch files if requested
        if config["backup"]["scan"]["touch"]:
            touchFiles(includeScanned+excludeScanned)

        # PRE - Execute files if requested
        if config["backup"]["scan"]["execute"]:
            executeFilesPre(includeScanned+excludeScanned)
            # Register post at exit
            atexit.register(executeFilesPost, (includeScanned+excludeScanned))

        # Add folders containing these files to include/exclude
        include += [os.path.dirname(file) for file in includeScanned]
        exclude += [os.path.dirname(file) for file in excludeScanned]

    command = generateCommand(config, include, exclude)

    runBackup(config, command)

    return 0

def setDefaultValues(config, configFile):
    if config['backup']['metadata']['file'] == '':
        config['backup']['metadata']['file'] = f"{configFile}.data"

    if config['backup']['scan']['cache']['file'] == '':
        config['backup']['scan']['cache']['file'] = f"{configFile}.cache"

def readArgs(args, short_options: str, long_options: list):
    try:
        arguments, values = getopt.getopt(sys.argv[1:], short_options, long_options)
        return arguments
    except getopt.error as err:
        print (str(err))
        sys.exit(2)

def parseCliArgs(args, data={}, argMap={}):

    # If argument-Map is empty, construct it
    if argMap == {}:
        for data_key, data_value in data:
            argMap[data_key] = [data_key]

    # Search in given arguments for argMap and write values
    for arg, val in args:
        # Special arguments
        if arg in ("-h", "--help"):
            global help_text
            print (help_text)
            sys.exit(0)

        found = False
        for map_key in argMap:
            map_altKeys = argMap[map_key]

            for altKey in map_altKeys:
                keyName = f"-{altKey}"
                # If name is longer, add second - for long-arg
                if len(altKey) > 1:
                    keyName = f"-{keyName}"
            
                # If argument matches
                if arg == keyName:
                    # If data expects bool write True, otherwise just write given value
                    # and stop for this arg
                    if map_key in data and isinstance(data[map_key], bool):
                        data[map_key] = True
                    else:
                        data[map_key] = val
                        
                    found = True
                    break
            if found:
                break

    return data

def setupLogging(verbose: bool):
    level = log.INFO
    format = '%(asctime)s %(levelname)s: %(message)s'
    
    if verbose:
        level = log.DEBUG

    log.basicConfig(stream=sys.stdout, format=format, level=level)

def readConfig(file: str):
    log.debug(f"Reading config '{file}'..")

    if not os.path.isfile(file):
        raise OSError(2, file)
    
    return readYamlFile(file)

def readYamlFile(file: str):
    with open(file, "r") as f:
        return yaml.load(f, Loader=yaml.FullLoader)

def checkRunConditions(config):
    log.debug("checkRunConditions")

    result = checkBatteryCharge(config)
    if not (isinstance(result, bool) and result):
        return result

    result = checkNetwork(config)
    if not (isinstance(result, bool) and result):
        return result

    return True

def checkBatteryCharge(config):
    log.debug("> Battery-charge")

    if not config["backup"]["run-conditions"]["battery"]["min-percent"] > 0:
        log.debug("Check disabled, skipping")
        return True

    # Stats-file-stream exists?
    if not os.path.exists("/sys/class/power_supply/battery/capacity"):
        log.warning("No battery found, skipping")
        return True

    # Read
    with open("/sys/class/power_supply/battery/capacity", "r") as file:
        battery_charge = int(file.readlines()[0])

        if battery_charge < config["backup"]["run-conditions"]["battery_min-percent"]:
            log.debug(f"Too low ({battery_charge} < {config['backup']['run-conditions']['battery']['min-percent']})")

            errorMsg = f"Battery-charge too low ({battery_charge} < {config['backup']['run-conditions']['battery']['min-percent']})"

            log.debug("or-AC-connected")
            if config["backup"]["run-conditions"]["battery"]["or_ac-connected"]:
                # Check battery-status
                battery_status = "Unknown"
                with open("/sys/class/power_supply/battery/status", "r") as file:
                    battery_status = file.readlines()[0].strip()
                if not battery_status == "Charging":
                    log.debug("Not charging ("+ battery_status +")")
                    errorMsg += " and not charging"

            return errorMsg
        else:
            log.debug(f"Ok ({battery_charge} < {config['backup']['run-conditions']['battery']['min-percent']})")
            return True

def checkNetwork(config):
    log.debug("Check Network")

    hostname = getHostnameFromRepository(config["borg"]["repository"])
    if hostname == None:
        log.debug("Repository does not have a hostname, skipping")
        return True


    result = checkNetworkHops(config, hostname)
    if not (isinstance(result, bool) and result):
        return result

    return True

def checkNetworkHops(config, hostname: str):
    log.debug("> Network-hops")

    if not config["backup"]["run-conditions"]["network"]["max_hops"] > 0:
        log.debug("Check disabled (0)")
        return True
    
    ip = resolveHostname(hostname)

    lastHopIp = traceroute(
        maxhops = config["backup"]["run-conditions"]["network"]["max_hops"],
        ip      = ip
        )[-2].strip()
    if(len(lastHopIp) >= 7):
        lastHopIp = lastHopIp.split(" ")[2]
    log.debug(f"LastHop-IP: {lastHopIp}")

    if ip != lastHopIp:
        return "Could not reach server in <= "+ str(config['backup']['run-conditions']['network']['max_hops']) +" hops"

    return True

def getHostnameFromRepository(repository: str):
    # Check if has hostname
    result = re.search(r'^(\w+@)?(([A-Z0-9-]+\.)+[A-Z0-9-]+):.*$', repository, flags= re.I)
    if result:
        return result[2]
    else:
        # No hostname
        return

def resolveHostname(hostname: str):
    log.debug(f"Resolving hostname {hostname}..")

    ip = socket.gethostbyname(hostname)

    log.debug(f"IP: {ip}")
    return ip

def traceroute(maxhops: int, ip: str):
    command = ["traceroute", "-n", f"-m {maxhops}", "-q 2", ip]

    log.debug(f"Running traceroute to ip={ip} with maxhops={maxhops}..")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = result.stdout.decode('utf-8').split('\n')

    log.debug(out)
    return out

def readBackupMetadata(config):
    file = config['backup']['metadata']['file']
    if os.path.isfile(file):
        log.debug(f"Reading metadata-file '{file}'..")
        return readYamlFile(file)
    return

def writeBackupMetadata(config, success: bool):
    data = {
        "time": int(time.time()),
        "success": success
    }

    log.debug(f"Writing metadata: {data}")
    with open(config['backup']['metadata']['file'], 'w') as file:
        yaml.dump(data, file)

def handleBackupMetadata(config, metadata):
    log.debug(f"Handling metadata..")

    age = int(time.time()-metadata["time"])
    if age <= config["backup"]["backup-age"]:
        if metadata["success"]:
            log.info("Backup-age is "+ str(age) +"s, next backup will be created in "+ str(age+config["backup"]["backup-age"]) +"s")
            quit()
        else:
            log.warning("Backup-age is "+ str(age) +"s, but was not successful!")

def scanForFolders(config):
    log.debug(f"Scan for marked folders")

    includeScannedFiles = []
    excludeScannedFiles = []

    # Read cache if exists
    if(os.path.isfile(config["backup"]["scan"]['cache']['file'])):
        log.debug(f"Cache-file found")
        
        cacheData = readYamlFile(config["backup"]["scan"]['cache']['file'])
        age = int(time.time()-cacheData["time"])

        log.debug(f"Cache-age: {str(age)}")

        if age <= config["backup"]["scan"]["cache"]["valid-time"]:
            log.info("Using Cache ("+ str(age) +"s old)")
            scan = False
            includeScannedFiles = cacheData["include"]
            excludeScannedFiles = cacheData["exclude"]

            log.debug(f"include: {includeScannedFiles}")
            log.debug(f"exclude: {excludeScannedFiles}")
            return includeScannedFiles, excludeScannedFiles

    # Scan folders for files
    log.info(f"Scanning for folders marked with {config['backup']['scan']['backup']} or {config['backup']['scan']['nobackup']}..")
    for location in config["backup"]["scan"]["locations"]:
        log.debug(f"Scanning {location}..")

        includeScannedFile = find(location, config["backup"]["scan"]["backup"])
        excludeScannedFile = find(location, config["backup"]["scan"]["nobackup"])

        if len(includeScannedFile) > 0:
            log.debug(f"include: {includeScannedFile}")
        if len(excludeScannedFile) > 0:
            log.debug(f"exclude: {excludeScannedFile}")

        includeScannedFiles += includeScannedFile
        excludeScannedFiles += excludeScannedFile

    # Write cache
    log.debug("Writing cache..")
    with open(config["backup"]["scan"]['cache']['file'], 'w') as file:
        yaml.dump(
            {
                "time": int(time.time()),
                "include": includeScannedFiles,
                "exclude": excludeScannedFiles
            }
            , file)

    log.info(f"Scan finished. {len(includeScannedFiles) + len(excludeScannedFiles)} files found")
    log.debug(f"include: {includeScannedFiles}")
    log.debug(f"exclude: {excludeScannedFiles}")
    return includeScannedFiles, excludeScannedFiles

def touchFiles(files: list):
    for file in files:
        if os.path.isfile(file):
            os.utime(file, None)

def executeFilesPre(files: list):
    log.info("Running script-pre..")
    
    # Store files which were successfully executed
    executedFiles = []

    try:
        for file in files:
            # Check for filesize and execution-permission
            if os.stat(file).st_size != 0 and os.access(file, os.X_OK):
                log.info(f"{file}")
                subprocess.run([file, "pre"], check=True)
                executedFiles += file

    except subprocess.CalledProcessError as err:
        log.critical("Script returned error, cannot continue!")
        executeFilesPost(executedFiles)
        raise err

def executeFilesPost(files: list):
    log.info("Running script-post..")
    
    for file in files:
        # Check for filesize and execution-permission
        if os.stat(file).st_size != 0 and os.access(file, os.X_OK):
            log.info(f"{file}")
            try:
                subprocess.run([file, "post"], check=True)
            except subprocess.CalledProcessError as err:
                log.error(err)
                pass

def generateEnviromentVars(config):
    args = config["borg"]["args"]
    envVars = [env_var +"='"+ config["borg"]["env-vars"][env_var] +"'" for env_var in config["borg"]["env-vars"]]
    return ' '.join(envVars)

def generateCommand(config, include, exclude):

    # Generate arguments        
    folderArgs = [f"'{folder}'" for folder in include]
    folderArgs += [f"--exclude '{folder}'" for folder in exclude]
    folderArgs = ' '.join(folderArgs)

    envVars = generateEnviromentVars(config)

    compression = config["borg"]["compression"]
    repository = config['borg']['repository']

    backupName = socket.getfqdn() +"__"+ datetime.datetime.now().strftime('%F_%T')

    return f"{envVars} borg create {config['borg']['args']} --exclude-caches --compression {compression} {repository}::{backupName} {folderArgs}"

def executeBorgCommand(config, command):

    envVars = generateEnviromentVars(config)

    repository = config['borg']['repository']

    borgSubcommand = command
    commandArgs = ''
    if ' ' in command:
        commandData = command.split(' ')
        borgSubcommand = commandData[0]
        commandArgs = ' '.join(commandData[1:])
    
    command = f"{envVars} borg {borgSubcommand} {repository} {commandArgs}"

    log.debug(f'Executing {command}')
    return os.system(command)

def runBackup(config, command):
    writeBackupMetadata(config, False)

    log.info("Running backup")
    for i in range(1, config["backup"]["tries"]["amount"]):
        log.debug(f"Try {i}")

        log.debug(command);
        code = os.system(command)
        log.debug(f"Return-code {code}")

        if not code == 0:
            log.error("Backup failed")
            
            if(i+1 != config["backup"]["tries"]["amount"]):
                print("- Retry in "+ str(config["backup"]["tries"]["sleep"]) +"s")
                time.sleep(config["backup"]["tries"]["sleep"])
        
        else:
            log.info("Backup successful")
            
            writeBackupMetadata(config, True)

            return
    
    log.critical("Backup completly failed!")



def find(path: str, searchName: str):
    result = subprocess.run(["find", path, "-type", "f", "-name", searchName], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.decode('utf-8').split('\n')[:-1]

def get_platform():
    platforms = {
        'linux1' : 'Linux',
        'linux2' : 'Linux',
        'darwin' : 'OS X',
        'win32' : 'Windows'
    }
    if sys.platform not in platforms:
        return sys.platform
    
    return platforms[sys.platform]




if __name__ == "__main__":
    sys.exit(main(sys.argv))
