#common utils for all controllers
import os
from glob import iglob
import json
import re
import sys
import subprocess
import watchdog.events
from shlex import quote
import postgresql as psql
from postgresql.exceptions import Exception as psqlException
from .watchdir import Observe
import atexit
import site
import webbrowser
import threading
from postgresql.exceptions import UndefinedTableError
from math import ceil
import operator

class dataObj(dict):
    def __init__(self, datafiles, sync):
        super().__init__()
        super().__setitem__(
            '_datafiles',
            {datafile:[] for datafile in datafiles}
        )
        self.sync = sync

    def __setitem__(self, key, value):
        if key not in self and key not in {k for parent in self['_datafiles'] for k in parent}:
            raise KeyError("Key %s has no associated file.  Use addKey() first"%key)
        super().__setitem__(key, value)

    def addKey(self, key, value, dest):
        """Adds a new root key to the app data storage object."""
        if dest not in self['_datafiles']:
            self['_datafiles'][dest] = [key]
        else:
            self['_datafiles'][dest].append(key)
        super().__setitem__(key, value)

    def save(self):
        """Saves the data object to the various data files"""
        self.sync.acquire()
        for datafile in self['_datafiles']:
            os.makedirs(os.path.dirname(datafile), exist_ok=True)
            writer = open(datafile, 'w')
            json.dump(
                {
                    key:self[key] for key in self['_datafiles'][datafile] if key in self
                },
                writer,
                indent='\t'
            )
            writer.close()
        self.sync.release()

_file_info = {
    'json': {
        'description': "Metadata regarding a specific run of pVAC-Seq",
        'visualizable': False,
    },
    'yml': {
        'description': "Manifest of auxiliary files supplied to pVAC-Seq",
        'visualizable': False,
    },
    'yaml': {
        'description': "Manifest of auxiliary files supplied to pVAC-Seq",
        'visualizable': False,
    },
    'log': {
        'description': "Transcript of messages produced by pVAC-Seq",
        'visualizable': False,
    },
    'chop.tsv': {
        'description': "Processed and filtered data, with peptide cleavage data added",
        'visualizable': True,
        'visualization_type': 'full',
    },
    'all_epitopes.tsv': {
        'description': "Processed data from IEDB, but with no filtering or extra data",
        'visualizable': True,
        'visualization_type': 'full',
    },
    'filtered.tsv': {
        'description': "Processed data with all filters applied",
        'visualizable': True,
        'visualization_type': 'full',
    },
    'stab.tsv': {
        'description': "Processed and filtered data, with peptide stability data added",
        'visualizable': True,
        'visualization_type': 'full',
    },
    'filtered.condensed.ranked.tsv': {
        'description': "A condensed report of the processed and filtered data, with ranking score added",
        'visualizable': True,
        'visualization_type': 'condensed',
    },
    'tsv': {
        'description': "Raw input data parsed out of the input vcf",
        'visualizable': False,
    },
    'vcf': {
        'description': "Unprocessed input VCF",
        'visualizable': False,
    },
}

def descriptions(ext):
    if ext in _file_info:
        return _file_info[ext]['description']
    elif re.search(r'(split|tsv)_\d+-\d+$', ext):
        return "A temporary file to cache a subset of the data when working with IEDB"
    elif re.search(r'key$', ext):
        return "Data used by pVAC-Seq to parse results from IEDB"
    return "Unknown File"

def is_visualizable(ext):
    if ext in _file_info:
        return _file_info[ext]['visualizable']
    else:
        return False

def visualization_type(ext):
    if ext in _file_info and 'visualization_type' in _file_info[ext]:
        return _file_info[ext]['visualization_type']
    else:
        return None

def column_filter(column):
    """standardize column names"""
    return column.replace(' ', '_').replace('-', '_').lower().strip()

def loaddata(datafiles, sync):
    sync.acquire()
    data = dataObj({datafiles[datafile] for datafile in datafiles if not datafile.endswith('-dir')}, sync)
    for datafile in data['_datafiles']:
        if os.path.isfile(datafile):
            try:
                current = json.load(open(datafile))
            except BaseException as e:
                #got to make sure that lock is released so we don't stall the app
                sync.release()
                raise e
            for (key, value) in current.items():
                data.addKey(key, value, datafile)
    sync.release()
    return data

def initialize(current_app, args):
    """Setup anything that needs to be configured before the app start"""
    #This section is run once, when the API spins up
    print("Initializing app configuration")
    #First, read all the json config files to load app configuration
    config = {'storage': {}}
    config_dir = os.path.join(
        os.path.dirname(__file__),
        '..',
        'config'
    )
    user_config_dir = os.path.expanduser("~/.pvacseq")
    if not os.path.isdir(user_config_dir):
        os.makedirs(user_config_dir)
    #For every config file predefined in the config directory,
    #first read and load the file, then
    #check the user config directory for an override
    for configfile in iglob(os.path.join(config_dir, '*.json')):
        reader = open(configfile)
        key = os.path.splitext(os.path.basename(configfile))[0]
        config[key] = json.load(reader)
        reader.close()
        try:
            reader = open(os.path.join(user_config_dir, os.path.basename(configfile)))
            if key == 'schema':
                config[key].update({
                    column_filter(k):v for (k,v) in json.load(reader).items()
                })
            else:
                config[key].update(json.load(reader))
            reader.close()
        except FileNotFoundError:
            pass
    for key in config['files']:
        config['files'][key] = os.path.abspath(os.path.expanduser(config['files'][key]))
    current_app.config.update(config) #save to the app configuration object

    #Now load the data object from the files specified in the configuration
    synchronizer = threading.RLock()
    data = loaddata(current_app.config['files'], synchronizer)
    if 'processid' not in data:
        data.addKey('processid', 0, current_app.config['files']['processes'])
    if 'dropbox' not in data:
        data.addKey('dropbox', {}, current_app.config['files']['dropbox'])
    #Check the last reboot (because pid's won't remain valid after a reboot)
    current_app.config['storage']['data'] = data
    import weakref
    current_app.config['storage']['loader'] = weakref.ref(current_app.config['storage']['data'])
    loader = current_app.config['storage']['loader']
    reboot = subprocess.check_output(['last', 'reboot']).decode().split("\n")[0]
    current_app.config['reboot'] = reboot
    if 'reboot' in data and data['reboot'] != reboot:
        print("A reboot has occurred since the server was first started")
        print(
            "pid's of old pVAC-Seq runs with id's",
            data['processid'],
            "and lower may be inaccurate"
        )
    current_app.config['storage']['children']={}
    current_app.config['storage']['manifest']={}

    visapp_path = os.path.relpath(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'visualizations.py'
        )
    )
    #Check if the bokeh port is already in use.  Attempt to reconnect?
    current_app.config['storage']['bokeh']=subprocess.Popen(
        'bokeh serve %s --allow-websocket-origin=localhost:8080'%(
            quote(visapp_path)
        ),
        shell=True,
        stdout=subprocess.DEVNULL
    )
    print(
        "Visualization server started on PID",
        current_app.config['storage']['bokeh'].pid
    )

    @atexit.register
    def cleanup_bokeh():
        print("Cleaning up visualization server")
        import signal
        current_app.config['storage']['bokeh'].send_signal(signal.SIGINT)
        try:
            current_app.config['storage']['bokeh'].wait(1)
        except subprocess.TimeoutExpired:
            current_app.config['storage']['bokeh'].terminate()

    #Establish a connection to the local postgres database
    try:
        tmp = psql.open("localhost/postgres")
    except psqlException as e:
        raise SystemExit("Unable to connect to your Postgres server.\
                         The pVAC-Seq API requires a running local Postgres server") from e
    if not len(tmp.prepare("SELECT 1 FROM pg_database WHERE datname = $1")('pvacseq')):
        tmp.execute("CREATE DATABASE pvacseq")
    tmp.close()
    db = psql.open("localhost/pvacseq")
    db.synchronizer = threading.RLock()
    current_app.config['storage']['db'] = db

    @atexit.register
    def cleanup_database():
        print("Cleaning up database connections")
        if 'db-clean' in current_app.config:
            with db.synchronizer:
                for table in current_app.config['db-clean']:
                    try:
                        current_app.config['storage']['db'].execute("DROP TABLE %s"%table)
                    except UndefinedTableError:
                        pass
        current_app.config['storage']['db'].close()

    #setup directory structure:
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'input'),
        exist_ok=True
    )
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'results'),
        exist_ok=True
    )
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'archive'),
        exist_ok=True
    )
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'dropbox'),
        exist_ok=True
    )
    os.makedirs(
        os.path.join(current_app.config['files']['data-dir'],'.tmp'),
        exist_ok=True
    )

    #Setup the watchers to observe the files
    current_app.config['storage']['watchers'] = []

    dbr = os.path.join(current_app.config['files']['data-dir'],'dropbox')
    dropbox_watcher = Observe(dbr)
    dropbox_watcher.subscribe(lambda x:print("Dropbox Event:", x))
    #Now we set up event handlers for the dropbox
    #This ensures that file ids are held consistent
    current = {
        os.path.join(path, filename)
        for (path, _, files) in os.walk(dbr)
        for filename in files
    }
    for (key, filename) in data['dropbox'].items():
        if type(data['dropbox'][key])==str:
            ext = '.'.join(os.path.basename(filename).split('.')[1:])
            print("Updating dropbox entry",key,"to new format")
            data['dropbox'][key] = {
                'fullname':os.path.join(
                    dbr,
                    filename
                ),
                'display_name':os.path.relpath(
                    filename,
                    dbr
                ),
                'description':descriptions(ext),
                'is_visualizable': is_visualizable(ext),
                'visualization_type': visualization_type(ext),
            }
    recorded = {item['fullname'] for item in data['dropbox'].values()}
    targets = {data['dropbox'][k]['fullname'] for k in data['dropbox'] if data['dropbox'][k]['fullname'] in recorded-current}
    for fileID in targets:
        del data['dropbox'][fileID]
    fileID = 0
    for filename in current-recorded:
        while str(fileID) in data['dropbox']:
            fileID += 1
        ext = '.'.join(os.path.basename(filename).split('.')[0b1:])
        print("Assigning file:", fileID,"-->",filename)
        data['dropbox'][str(fileID)] = {
            'fullname':os.path.abspath(os.path.join(
                dbr,
                filename
            )),
            'display_name':os.path.relpath(
                filename,
                dbr
            ),
            'description':descriptions(ext),
            'is_visualizable': is_visualizable(ext),
            'visualization_type': visualization_type(ext),
        }

    data_path = current_app.config['files']
    def _create(event):
        data = loader()
        filename = os.path.relpath(
            event.src_path,
            dbr
        )
        fileID = 0
        while str(fileID) in data['dropbox']:
            fileID += 1
        ext = '.'.join(os.path.basename(filename).split('.')[0b1:])
        print("Creating file:", fileID, "-->",filename)
        data['dropbox'][str(fileID)] = {
            'fullname':os.path.abspath(os.path.join(
                dbr,
                filename
            )),
            'display_name':filename,
            'description':descriptions(ext),
            'is_visualizable': is_visualizable(ext),
            'visualization_type': visualization_type(ext),
        }
        data.save()
    dropbox_watcher.subscribe(
        _create,
        watchdog.events.FileCreatedEvent
    )

    def _delete(event):
        data = loader()
        filename = os.path.relpath(
            event.src_path,
            dbr
        )
        for key in list(data['dropbox']):
            if data['dropbox'][key]['display_name'] == filename:
                del data['dropbox'][key]
                print("Deleting file:",key,'-->', filename)
                with db.synchronizer:
                    query = db.prepare("SELECT 1 FROM information_schema.tables WHERE table_name = $1")
                    if len(query('data_dropbox_'+str(key))):
                        db.execute("DROP TABLE data_dropbox_"+str(key))
                data.save()
                return
    dropbox_watcher.subscribe(
        _delete,
        watchdog.events.FileDeletedEvent
    )

    def _move(event):
        data = loader()
        filesrc = os.path.relpath(
            event.src_path,
            dbr
        )
        filedest = os.path.relpath(
            event.dest_path,
            dbr
        )
        ext = '.'.join(os.path.basename(filedest).split('.')[0b1:])
        for key in data['dropbox']:
            if data['dropbox'][key]['display_name'] == filesrc:
                data['dropbox'][key] = {
                    'fullname':os.path.abspath(os.path.join(
                        dbr,
                        filedest
                    )),
                    'display_name':os.path.relpath(
                        filedest,
                        dbr
                    ),
                    'description':descriptions(ext),
                    'is_visualizable': is_visualizable(ext),
                    'visualization_type': visualization_type(ext),
                }
                print("Moving file:", key,'(',filesrc,'-->',filedest,')')
                data.save()
                return
    dropbox_watcher.subscribe(
        _move,
        watchdog.events.FileMovedEvent
    )
    current_app.config['storage']['watchers'].append(dropbox_watcher)

    resultdir = os.path.join(current_app.config['files']['data-dir'], 'results')
    results_watcher = Observe(resultdir)
    results_watcher.subscribe(lambda x:print("Results Event:", x))
    for processID in range(data['processid']+1):
        processkey = 'process-%d'%processID
        if processkey in data:
            print("Checking files for process", processID)
            if 'files' in data[processkey]:
                if type(data[processkey]['files']) == list:
                    print("Updating file manifest of process",processID,"to new format")
                    data[processkey]['files']={
                        fileID:{
                            'fullname':filename,
                            'display_name':os.path.relpath(
                                filename,
                                data[processkey]['output']
                            ),
                            'description':descriptions(
                                '.'.join(os.path.basename(filename).split('.')[1:])
                            ),
                            'is_visualizable': is_visualizable(
                                '.'.join(os.path.basename(filename).split('.')[1:])
                            ),
                            'visualization_type': visualization_type(
                                '.'.join(os.path.basename(filename).split('.')[1:])
                            ),
                        }
                        for (filename, fileID) in zip(
                            data[processkey]['files'],
                            range(sys.maxsize)
                        )
                    }
            else:
                data[processkey]['files'] = {}
            current = {
                os.path.join(path, filename)
                for (path, _, files) in os.walk(data[processkey]['output'])
                for filename in files
            }
            recorded = {entry['fullname']:k for k,entry in data[processkey]['files'].items()}
            for fileID in recorded.keys()-current:
                print("Deleting file",fileID,"from manifest")
                fileID = recorded[fileID]
                del data[processkey]['files'][fileID]
            for filename in current-recorded.keys():
                fileID = len(data[processkey]['files'])
                while str(fileID) in data[processkey]['files']:
                    fileID += 1
                fileID = str(fileID)
                ext = '.'.join(os.path.basename(filename).split('.')[1:])
                print("Assigning file:",fileID,"-->",filename)
                data[processkey]['files'][fileID] = {
                    'fullname':filename,
                    'display_name':os.path.relpath(
                        filename,
                        data[processkey]['output']
                    ),
                    'description':descriptions(ext),
                    'is_visualizable': is_visualizable(ext),
                    'visualization_type': visualization_type(ext),
                }

    def _create(event):
        data = loader()
        parentpaths = {
            (data['process-%d'%i]['output'], i)
            for i in range(data['processid']+1)
            if 'process-%d'%i in data
        }
        filepath = event.src_path
        for (parentpath, parentID) in parentpaths:
            if os.path.commonpath([filepath, parentpath])==parentpath:
                print("New output from process",parentID)
                processkey = 'process-%d'%parentID
                fileID = len(data[processkey]['files'])
                while str(fileID) in data[processkey]['files']:
                    fileID+=1
                fileID = str(fileID)
                display_name = os.path.relpath(
                    filepath,
                    data[processkey]['output']
                )
                ext = '.'.join(os.path.basename(filepath).split('.')[1:])
                print("Assigning id",fileID,'-->',display_name)
                data[processkey]['files'][fileID] = {
                    'fullname':filepath,
                    'display_name':display_name,
                    'description':descriptions(ext),
                    'is_visualizable': is_visualizable(ext),
                    'visualization_type': visualization_type(ext),
                }
                data.save()
                return
    results_watcher.subscribe(
        _create,
        watchdog.events.FileCreatedEvent
    )

    def _delete(event):
        data = loader()
        parentpaths = {
            (data['process-%d'%i]['output'], i)
            for i in range(data['processid']+1)
            if 'process-%d'%i in data
        }
        filepath = event.src_path
        for (parentpath, parentID) in parentpaths:
            if os.path.commonpath([filepath, parentpath])==parentpath:
                print("Deleted output from process",parentID)
                processkey = 'process-%d'%parentID
                for (fileID, filedata) in list(data[processkey]['files'].items()):
                    if filedata['fullname'] == filepath:
                        del data[processkey]['files'][fileID]
                        print("Deleted file:", fileID,'-->',filepath)
                        with db.synchronizer:
                            query = db.prepare("SELECT 1 FROM information_schema.tables WHERE table_name = $1")
                            if len(query('data_%d_%s'%(parentID, fileID))):
                                db.execute("DROP TABLE data_%d_%s"%(parentID, fileID))
                data.save()
                return
    results_watcher.subscribe(
        _delete,
        watchdog.events.FileDeletedEvent
    )

    def _move(event):
        data = loader()
        filesrc = event.src_path
        filedest = event.dest_path
        parentpaths = {
            (data['process-%d'%i]['output'], i)
            for i in range(data['processid']+1)
            if 'process-%d'%i in data
        }
        srckey = ''
        destkey = ''
        for (parentpath, parentID) in parentpaths:
            if os.path.commonpath([filesrc, parentpath])==parentpath:
                srckey = 'process-%d'%parentID
            elif os.path.commonpath([filedest, parentpath]) == parentpath:
                destkey = 'process-%d'%parentID

        ext = '.'.join(os.path.basename(filedest).split('.')[1:])
        if srckey == destkey:
            for (fileID, filedata) in data[srckey]['files'].items():
                if filedata['fullname'] == filesrc:
                    data[srckey]['files'][fileID] = {
                        'fullname':filedest,
                        'display_name':os.path.relpath(
                            filedest,
                            data[srckey]['output']
                        ),
                        'description':descriptions(ext),
                        'is_visualizable': is_visualizable(ext),
                        'visualization_type': visualization_type(ext),
                    }
        else:
            _delete(event)
            evt = lambda:None
            evt.src_path = event.dest_path
            _create(evt)
    results_watcher.subscribe(
        _move,
        watchdog.events.FileMovedEvent
    )
    current_app.config['storage']['watchers'].append(results_watcher)


    @atexit.register
    def cleanup_watchers():
        print("Cleaning up observers")
        for watcher in current_app.config['storage']['watchers']:
            watcher.stop()
            watcher.join()

    current_app.config['storage']['synchronizer'] = synchronizer
    data.save()

    if '--nogui' not in args:
        #Attempt to boot the frontend api
        site_dirs = site.getsitepackages()
        for path in site_dirs:
            tmp_path = os.path.join(
                path,
                'pvacseq-client'
            )
            if os.path.isdir(tmp_path):
                current_app.config['storage']['client-dir'] = tmp_path
                break
        if 'client-dir' not in current_app.config['storage']:
            sys.exit("Unable to locate the frontend!")
        print("Launching Frontend Server")
        current_app.config['storage']['frontend']=subprocess.Popen(
            [
                sys.executable,
                '-m',
                'http.server',
                '8000'
            ],
            cwd=current_app.config['storage']['client-dir']
        )

        @atexit.register
        def cleanup_frontend():
            print("Cleaning up frontend server")
            import signal
            current_app.config['storage']['frontend'].send_signal(signal.SIGINT)
            try:
                current_app.config['storage']['frontend'].wait(1)
            except subprocess.TimeoutExpired:
                current_app.config['storage']['frontend'].terminate()

        #Uncomment if we want to open a browser in the frontend
        # threading.Timer(2.5, lambda :webbrowser.open('http://localhost:8000')).start()

    print("Initialization complete.  Booting API")


### filtering, sorting, and paging functions shared by multiple files ###
queryfilters = re.compile(r'(.+)(<=?|>=?|!=|==)(.+)')

ops = {
    '<': operator.lt,
    '<=': operator.le,
    '==': operator.eq,
    '!=': operator.ne,
    '>=': operator.ge,
    '>': operator.gt
}

# see if string is a number
def is_number(string):
    try:
        float(string)
        return True
    except ValueError:
        return False

def cmp(arg1, op, arg2):
    operation = ops.get(op)
    return operation(arg1,arg2)

def fullresponse(data, page, count):
    if count == -1:
        count = len(data)
    if count == 0:
        total_pages = 0
    else:
        total_pages = ceil(len(data)/count)
    return ({
        "_meta": {
            "current_page":page,
            "per_page":count,
            "total_pages":total_pages,
            "total_count":len(data)
        },
        "result": data[(count*(page-1)):((count*page)) if (count*page)<len(data) else len(data)]
    })

def sort(data, sorting, page, count, columns):
    if not len(sorting) or sorting[0]=="none":
        return fullresponse(data, page, count)
    i = len(sorting)-1
    while i > -1:
        col = sorting[i]
        if not col.startswith('-') and not col.startswith('+'):
            return ({
                "code": 400,
                "message": "Please indicate which direction you'd like to sort by by putting a + or - in front of the column name",
                "fields": "sorting"
            }, 400)
        if col[1:] not in columns:
            return ({
                "code": 400,
                "message": "Unknown column name %s" % col[1:],
                "fields": "sorting"
            }, 400)
        data = sorted(data, key=operator.itemgetter(col[1:]), reverse=True if col.startswith('-') else False)
        i-=1
    return fullresponse(data, page, count)

def filterdata(data, filters, sorting, page, count):
    if not len(data):
        return fullresponse(data, page, count)
    columns = [name for name in data[0]]
    if not len(filters) or filters[0]=="none":
        return sort(data, sorting, page, count, columns)
    filteredlist = []
    for i in range(len(data)):
        comparisons = []
        for j in range(len(filters)):
            f = filters[j].strip()
            if not len(f):
                continue
            result = queryfilters.match(f)
            if not result:
                return ({
                    "code":400,
                    "message": "Encountered an invalid filter (%s)" % f,
                    "fields": "filtering"
                }, 400)
            colname = result.group(1)
            if colname not in columns:
                return ({
                    "code": 400,
                    "message": "Unknown column name %s" % result.group(1),
                    "fields": "filtering"
                }, 400)
            op = result.group(2)
            val = result.group(3)
            comp = data[i][colname]
            if type(comp) == int:
                val = int(val)
            # see if string is actually a number for accurate number comparisons,
            # avoiding string comparisons of numbers in cmp() function
            elif is_number(comp):
                data[i]
                val = float(val)
            if not cmp(comp, op, val):
                break
            if j == len(filters)-1:
                filteredlist.append(data[i])
    return sort(filteredlist, sorting, page, count, columns)
