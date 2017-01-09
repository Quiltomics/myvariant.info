# LOGGING #
import logging, os, datetime, time

LOGGER_NAME = "myvariant.hub"
def setup_default_log(log_folder):
    # this will affect any logging calls
    logging.basicConfig(level=logging.DEBUG)
    logfile = os.path.join(log_folder, '%s_%s_hub.log' % (LOGGER_NAME,time.strftime("%Y%m%d",datetime.datetime.now().timetuple())))
    fh = logging.FileHandler(logfile)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(process)d:%(threadName)s] - %(name)s - %(levelname)s -- %(message)s',datefmt="%H:%M:%S"))
    fh.name = "logfile"
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    if not fh.name in [h.name for h in logger.handlers]:
        logger.addHandler(fh)
    print("ok setup%s" % logger)
    return logger


ALLOWED_OPTIONS = ['_source', 'start', 'from_', 'size', 'fields',
                   'sort', 'explain', 'version', 'facets', 'fetch_all']

ES_QUERY_MODULE = "www.api.es"

STATUS_CHECK_ID = 'chr1:g.218631822G>A'

FIELD_NOTES_PATH = 'www/context/myvariant_field_table_notes.json'
JSONLD_CONTEXT_PATH = 'www/context/context.json'


# *****************************************************************************
# Google Analytics Settings
# *****************************************************************************
# Google Analytics Account ID
GA_ACCOUNT = ''
# Turn this to True to start google analytics tracking
GA_RUN_IN_PROD = False

# 'category' in google analytics event object
GA_EVENT_CATEGORY = 'v1_api'
# 'action' for get request in google analytics event object
GA_EVENT_GET_ACTION = 'get'
# 'action' for post request in google analytics event object
GA_EVENT_POST_ACTION = 'post'
# url for google analytics tracker
GA_TRACKER_URL = 'MyVariant.info'

# *****************************************************************************
# URL settings
# *****************************************************************************
# For URL stuff
ANNOTATION_ENDPOINT = 'variant'
QUERY_ENDPOINT = 'query'
API_VERSION = 'v1'
# TODO Fill in a status id here
STATUS_CHECK_ID = 'chr1:g.218631822G>A'
# Path to a file containing a json object with information about elasticsearch fields
FIELD_NOTES_PATH = 'www/context/myvariant_field_table_notes.json'
JSONLD_CONTEXT_PATH = 'www/context/context.json'
NOSETEST_SETTINGS = 'tests.nosetest_config'

HOST_ENVAR_NAME = "MV_HOST"

HG38_FIELDS = ['clinvar.hg38', 'dbnsfp.hg38', 'evs.hg38']
HG19_FIELDS = ['clinvar.hg19', 'cosmic.hg19', 'dbnsfp.hg19', 'dbsnp.hg19', 'docm.hg19', 'evs.hg19', 'grasp.hg19'] #, 'mutdb.hg19', 'wellderly.hg19']

# ################### #
# MYVARIANT HUB VARS  #
# ################### #

DATA_SRC_MASTER_COLLECTION = 'src_master'   # for metadata of each src collections
DATA_SRC_DUMP_COLLECTION = 'src_dump'       # for src data download information
DATA_SRC_BUILD_COLLECTION = 'src_build'     # for src data build information

DATA_TARGET_MASTER_COLLECTION = 'db_master'

# time in seconds for dispatcher to check new jobs
DISPATCHER_SLEEP_TIME = 1
# storage class to be used by uploader script
SOURCE_MANAGER_CLASS = None # use default one
# where to store info about processes launched by the hub
RUN_DIR = './run'

# define valid sources to get chrom from, and for each, name of the chrom field
CHROM_FIELDS = {'cadd':'chrom', 'clinvar':'chrom', 'cosmic':'chrom', 'dbnsfp':'chrom',
                'dbsnp':'chrom', 'docm':'chrom', 'evs':'chrom', 'exac':'chrom'}
