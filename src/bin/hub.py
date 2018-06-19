#!/usr/bin/env python

import asyncio, asyncssh, sys, os, copy
import concurrent.futures
# see https://github.com/biothings/biothings.api/issues/5 (trying...)
#import multiprocessing_on_dill
#concurrent.futures.process.multiprocessing = multiprocessing_on_dill
from functools import partial

from collections import OrderedDict

import config, biothings
biothings.config_for_app(config)

import logging
# shut some mouths...
logging.getLogger("elasticsearch").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)
logging.getLogger("boto").setLevel(logging.ERROR)

logging.info("Hub DB backend: %s" % biothings.config.HUB_DB_BACKEND)
logging.info("Hub database: %s" % biothings.config.DATA_HUB_DB_DATABASE)

from biothings.utils.manager import JobManager
loop = asyncio.get_event_loop()
job_manager = JobManager(loop,num_workers=config.HUB_MAX_WORKERS,
                      num_threads=config.HUB_MAX_THREADS,
                      max_memory_usage=config.HUB_MAX_MEM_USAGE)


import hub.dataload
import biothings.hub.dataload.uploader as uploader
import biothings.hub.dataload.dumper as dumper
import biothings.hub.dataload.source as source
import biothings.hub.databuild.builder as builder
import biothings.hub.databuild.differ as differ
import biothings.hub.databuild.syncer as syncer
import biothings.hub.dataindex.indexer as indexer
import biothings.hub.datainspect.inspector as inspector
from biothings.hub.api.manager import APIManager
from hub.databuild.builder import MyVariantDataBuilder
from hub.databuild.mapper import TagObserved
from hub.dataindex.indexer import VariantIndexer
from biothings.utils.hub import schedule, pending, done, CompositeCommand, \
                                start_server, HubShell, CommandDefinition

shell = HubShell(job_manager)

# will check every 10 seconds for sources to upload
upload_manager = uploader.UploaderManager(poll_schedule = '* * * * * */10', job_manager=job_manager)
dmanager = dumper.DumperManager(job_manager=job_manager)
sources_path = hub.dataload.__sources_dict__
smanager = source.SourceManager(sources_path,dmanager,upload_manager)

dmanager.schedule_all()
upload_manager.poll('upload',lambda doc: shell.launch(partial(upload_manager.upload_src,doc["_id"])))

# deal with 3rdparty datasources
import biothings.hub.dataplugin.assistant as assistant
from biothings.hub.dataplugin.manager import DataPluginManager
dp_manager = DataPluginManager(job_manager=job_manager)
assistant_manager = assistant.AssistantManager(data_plugin_manager=dp_manager,
                                               dumper_manager=dmanager,
                                               uploader_manager=upload_manager,
                                               job_manager=job_manager)
# register available plugin assitant
assistant_manager.configure()
# load existing plugins
assistant_manager.load()

observed = TagObserved(name="observed")
build_manager = builder.BuilderManager(
        builder_class=partial(MyVariantDataBuilder,mappers=[observed]),
        job_manager=job_manager)
build_manager.configure()

diff_manager = differ.DifferManager(job_manager=job_manager)
diff_manager.configure([differ.ColdHotSelfContainedJsonDiffer,differ.SelfContainedJsonDiffer])

inspector = inspector.InspectorManager(upload_manager=upload_manager,
                                       build_manager=build_manager,
                                       job_manager=job_manager)

from biothings.hub.databuild.syncer import ThrottledESColdHotJsonDiffSelfContainedSyncer, ThrottledESJsonDiffSelfContainedSyncer, \
                                           ESColdHotJsonDiffSelfContainedSyncer, ESJsonDiffSelfContainedSyncer
sync_manager = syncer.SyncerManager(job_manager=job_manager)
sync_manager.configure(klasses=[ESColdHotJsonDiffSelfContainedSyncer,ESJsonDiffSelfContainedSyncer])

sync_manager_prod = syncer.SyncerManager(job_manager=job_manager)
sync_manager_prod.configure(klasses=[partial(ThrottledESColdHotJsonDiffSelfContainedSyncer,config.MAX_SYNC_WORKERS),
                                       partial(ThrottledESJsonDiffSelfContainedSyncer,config.MAX_SYNC_WORKERS)])

index_manager = indexer.IndexerManager(job_manager=job_manager)
index_manager.configure(config.ES_CONFIG)

# API manager: used to run API instances from the hub
api_manager = APIManager()

# let's glue everything together
managers = {
        "dump_manager" : dmanager,
        "upload_manager" : upload_manager,
        "source_manager" : smanager,
        "build_manager" : build_manager,
        "diff_manager" : diff_manager,
        "index_manager" : index_manager,
        "dataplugin_manager" : dp_manager,
        "assistant_manager" : assistant_manager,
        "inspect_manager" : inspector,
        "sync_manager" : sync_manager,
        "api_manager" : api_manager,
        }
shell.register_managers(managers)

from biothings.utils.hub import HubReloader
reloader = HubReloader(["hub/dataload/sources","plugins"],
                       [smanager,assistant_manager],
                       reload_func=partial(shell.restart,force=True))
reloader.monitor()

import biothings.utils.mongo as mongo
def snpeff(build_name=None,sources=[], force_use_cache=True):
    """
    Shortcut to run snpeff on all sources given a build_name
    or a list of source names will process sources one by one
    Since it's particularly useful when snpeff data needs reprocessing

    force_use_cache=True is used to make sure all cache files are used to
    speed up, while source is actually being postprocessed. We're assuming
    data hasn't changed and there's no new _ids since the last time source
    was processed.
    """
    if build_name:
        sources = mongo.get_source_fullnames(build_manager.list_sources(build_name))
    else:
        sources = mongo.get_source_fullnames(sources)
    # remove any snpeff related collection
    sources = [src for src in sources if not src.startswith("snpeff")]
    config.logger.info("Sequentially running snpeff on %s" % repr(sources))
    @asyncio.coroutine
    def do(srcs):
        for src in srcs:
            config.logger.info("Running snpeff on '%s'" % src)
            job = upload_manager.upload_src(src,steps="post",force_use_cache=force_use_cache)
            yield from asyncio.wait(job)
    task = asyncio.ensure_future(do(sources))
    return task

def rebuild_cache(build_name=None,sources=None,target=None,force_build=False):
    """Rebuild cache files for all sources involved in build_name, as well as 
    the latest merged collection found for that build"""
    if build_name:
        sources = mongo.get_source_fullnames(build_manager.list_sources(build_name))
        target = mongo.get_latest_build(build_name)
    elif sources:
        sources = mongo.get_source_fullnames(sources)
    if not sources and not target:
        raise Exception("No valid sources found")

    def rebuild(col):
        cur = mongo.id_feeder(col,batch_size=10000,logger=config.logger,force_build=force_build)
        [i for i in cur] # just iterate

    @asyncio.coroutine
    def do(srcs,tgt):
        pinfo = {"category" : "cache",
                "source" : None,
                "step" : "rebuild",
                "description" : ""}
        config.logger.info("Rebuild cache for sources: %s, target: %s" % (srcs,tgt))
        for src in srcs:
            # src can be a full name (eg. clinvar.clinvar_hg38) but id_feeder knows only name (clinvar_hg38)
            if "." in src:
                src = src.split(".")[1]
            config.logger.info("Rebuilding cache for source '%s'" % src)
            col = mongo.get_src_db()[src]
            pinfo["source"] = src
            job = yield from job_manager.defer_to_thread(pinfo, partial(rebuild,col))
            yield from job
            config.logger.info("Done rebuilding cache for source '%s'" % src)
        if tgt:
            config.logger.info("Rebuilding cache for target '%s'" % tgt)
            col = mongo.get_target_db()[tgt]
            pinfo["source"] = tgt
            job = job_manager.defer_to_thread(pinfo, partial(rebuild,col))
            yield from job

    task = asyncio.ensure_future(do(sources,target))
    return task

COMMANDS = OrderedDict()
# getting info
COMMANDS["source_info"] = CommandDefinition(command=smanager.get_source,tracked=False)
COMMANDS["status"] = CommandDefinition(command=shell.status,tracked=False)
# dump commands
COMMANDS["dump"] = dmanager.dump_src
COMMANDS["dump_all"] = dmanager.dump_all
# upload commands
COMMANDS["upload"] = upload_manager.upload_src
COMMANDS["upload_all"] = upload_manager.upload_all
COMMANDS["snpeff"] = snpeff
COMMANDS["rebuild_cache"] = rebuild_cache
# building/merging
COMMANDS["whatsnew"] = build_manager.whatsnew
COMMANDS["lsmerge"] = build_manager.list_merge
COMMANDS["rmmerge"] = build_manager.delete_merge
COMMANDS["merge"] = build_manager.merge
COMMANDS["premerge"] = partial(build_manager.merge,steps=["merge","metadata"])
COMMANDS["es_sync_hg19_test"] = partial(sync_manager.sync,"es",
                                        target_backend=(config.ES_CONFIG["env"]["test"]["host"],
                                                        config.ES_CONFIG["env"]["test"]["index"]["hg19"][0]["index"],
                                                        config.ES_CONFIG["env"]["test"]["index"]["hg19"][0]["doc_type"]))
COMMANDS["es_sync_hg38_test"] = partial(sync_manager.sync,"es",
                                        target_backend=(config.ES_CONFIG["env"]["test"]["host"],
                                                        config.ES_CONFIG["env"]["test"]["index"]["hg38"][0]["index"],
                                                        config.ES_CONFIG["env"]["test"]["index"]["hg38"][0]["doc_type"]))
COMMANDS["es_sync_hg19_prod"] = partial(sync_manager_prod.sync,"es",
                                        target_backend=(config.ES_CONFIG["env"]["prod"]["host"],
                                                        config.ES_CONFIG["env"]["prod"]["index"]["hg19"][0]["index"],
                                                        config.ES_CONFIG["env"]["prod"]["index"]["hg19"][0]["doc_type"]))
COMMANDS["es_sync_hg38_prod"] = partial(sync_manager_prod.sync,"es",
                                        target_backend=(config.ES_CONFIG["env"]["prod"]["host"],
                                                        config.ES_CONFIG["env"]["prod"]["index"]["hg38"][0]["index"],
                                                        config.ES_CONFIG["env"]["prod"]["index"]["hg38"][0]["doc_type"]))
COMMANDS["es_config"] = config.ES_CONFIG
# diff
COMMANDS["diff"] = diff_manager.diff
COMMANDS["diff_demo"] = partial(diff_manager.diff,differ.SelfContainedJsonDiffer.diff_type)
COMMANDS["diff_hg38"] = partial(diff_manager.diff,differ.SelfContainedJsonDiffer.diff_type)
COMMANDS["diff_hg19"] = partial(diff_manager.diff,differ.ColdHotSelfContainedJsonDiffer.diff_type)
COMMANDS["report"] = diff_manager.diff_report
COMMANDS["release_note"] = diff_manager.release_note
COMMANDS["publish_diff_hg19"] = partial(diff_manager.publish_diff,config.S3_APP_FOLDER + "-hg19")
COMMANDS["publish_diff_hg38"] = partial(diff_manager.publish_diff,config.S3_APP_FOLDER + "-hg38")
# indexing commands
COMMANDS["index"] = index_manager.index
COMMANDS["snapshot"] = index_manager.snapshot
COMMANDS["snapshot_demo"] = partial(index_manager.snapshot,repository=config.SNAPSHOT_REPOSITORY + "-demo")
COMMANDS["publish_snapshot_hg19"] = partial(index_manager.publish_snapshot,config.S3_APP_FOLDER + "-hg19")
COMMANDS["publish_snapshot_hg38"] = partial(index_manager.publish_snapshot,config.S3_APP_FOLDER + "-hg38")
# inspector
COMMANDS["inspect"] = inspector.inspect
# demo
COMMANDS["publish_diff_demo_hg19"] = partial(diff_manager.publish_diff,config.S3_APP_FOLDER + "-demo_hg19",
                                        s3_bucket=config.S3_DIFF_BUCKET + "-demo")
COMMANDS["publish_diff_demo_hg38"] = partial(diff_manager.publish_diff,config.S3_APP_FOLDER + "-demo_hg38",
                                        s3_bucket=config.S3_DIFF_BUCKET + "-demo")
COMMANDS["publish_snapshot_demo_hg19"] = partial(index_manager.publish_snapshot,config.S3_APP_FOLDER + "-demo_hg19",
                                                                                ro_repository=config.READONLY_SNAPSHOT_REPOSITORY + "-demo")
COMMANDS["publish_snapshot_demo_hg38"] = partial(index_manager.publish_snapshot,config.S3_APP_FOLDER + "-demo_hg38",
                                                                                ro_repository=config.READONLY_SNAPSHOT_REPOSITORY + "-demo")
# data plugins
COMMANDS["register_url"] = partial(assistant_manager.register_url)
COMMANDS["unregister_url"] = partial(assistant_manager.unregister_url)
COMMANDS["dump_plugin"] = dp_manager.dump_src

# admin/advanced
from biothings.utils.jsondiff import make as jsondiff
EXTRA_NS = {
        "dm" : CommandDefinition(command=dmanager,tracked=False),
        "dpm" : CommandDefinition(command=dp_manager,tracked=False),
        "am" : CommandDefinition(command=assistant_manager,tracked=False),
        "um" : CommandDefinition(command=upload_manager,tracked=False),
        "bm" : CommandDefinition(command=build_manager,tracked=False),
        "dim" : CommandDefinition(command=diff_manager,tracked=False),
        "sm" : CommandDefinition(command=sync_manager,tracked=False),
        "im" : CommandDefinition(command=index_manager,tracked=False),
        "jm" : CommandDefinition(command=job_manager,tracked=False),
        "ism" : CommandDefinition(command=inspector,tracked=False),
        "api" : CommandDefinition(command=api_manager,tracked=False),
        "mongo_sync" : CommandDefinition(command=partial(sync_manager.sync,"mongo"),tracked=False),
        "sync" : CommandDefinition(command=sync_manager.sync),
        "loop" : CommandDefinition(command=loop,tracked=False),
        "pqueue" : CommandDefinition(command=job_manager.process_queue,tracked=False),
        "tqueue" : CommandDefinition(command=job_manager.thread_queue,tracked=False),
        "g" : CommandDefinition(command=globals(),tracked=False),
        "sch" : CommandDefinition(command=partial(schedule,loop),tracked=False),
        "top" : CommandDefinition(command=job_manager.top,tracked=False),
        "pending" : CommandDefinition(command=pending,tracked=False),
        "done" : CommandDefinition(command=done,tracked=False),
        # required by API only (just fyi)
        "builds" : CommandDefinition(command=build_manager.build_info,tracked=False),
        "build" : CommandDefinition(command=lambda id: build_manager.build_info(id=id),tracked=False),
        "job_info" : CommandDefinition(command=job_manager.job_info,tracked=False),
        "dump_info" : CommandDefinition(command=dmanager.dump_info,tracked=False),
        "upload_info" : CommandDefinition(command=upload_manager.upload_info,tracked=False),
        "build_config_info" : CommandDefinition(command=build_manager.build_config_info,tracked=False),
        "index_info" : CommandDefinition(command=index_manager.index_info,tracked=False),
        "diff_info" : CommandDefinition(command=diff_manager.diff_info,tracked=False),
        "commands" : CommandDefinition(command=shell.command_info,tracked=False),
        "command" : CommandDefinition(command=lambda id,*args,**kwargs: shell.command_info(id=id,*args,**kwargs),tracked=False),
        "sources" : CommandDefinition(command=smanager.get_sources,tracked=False),
        "source_save_mapping" : CommandDefinition(command=smanager.save_mapping),
        "build_save_mapping" : CommandDefinition(command=build_manager.save_mapping),
        "validate_mapping" : CommandDefinition(command=index_manager.validate_mapping),
        "jsondiff" : CommandDefinition(command=jsondiff,tracked=False),
        "create_build_conf" : CommandDefinition(command=build_manager.create_build_configuration),
        "delete_build_conf" : CommandDefinition(command=build_manager.delete_build_configuration),
        "get_apis" : CommandDefinition(command=api_manager.get_apis,tracked=False),
        "delete_api" : CommandDefinition(command=api_manager.delete_api),
        "create_api" : CommandDefinition(command=api_manager.create_api),
        "start_api" : CommandDefinition(command=api_manager.start_api),
        "stop_api" : api_manager.stop_api,

}

import tornado.web
from biothings.hub.api import generate_api_routes, EndpointDefinition

API_ENDPOINTS = {
        # extra commands for API
        "builds" : EndpointDefinition(name="builds",method="get"),
        "build" : [EndpointDefinition(method="get",name="build"),
                   EndpointDefinition(method="delete",name="rmmerge"),
                   EndpointDefinition(name="merge",method="put",suffix="new"),
                   EndpointDefinition(name="build_save_mapping",method="put",suffix="mapping"),
                   ],
        "diff" : EndpointDefinition(name="diff",method="put",force_bodyargs=True),
        "job_manager" : EndpointDefinition(name="job_info",method="get"),
        "dump_manager": EndpointDefinition(name="dump_info", method="get"),
        "upload_manager" : EndpointDefinition(name="upload_info",method="get"),
        "build_manager" : EndpointDefinition(name="build_config_info",method="get"),
        "index_manager" : EndpointDefinition(name="index_info",method="get"),
        "diff_manager" : EndpointDefinition(name="diff_info",method="get"),
        "commands" : EndpointDefinition(name="commands",method="get"),
        "command" : EndpointDefinition(name="command",method="get"),
        "sources" : EndpointDefinition(name="sources",method="get"),
        "source" : [EndpointDefinition(name="source_info",method="get"),
                    EndpointDefinition(name="dump",method="put",suffix="dump"),
                    EndpointDefinition(name="upload",method="put",suffix="upload"),
                    EndpointDefinition(name="source_save_mapping",method="put",suffix="mapping")],
        "inspect" : EndpointDefinition(name="inspect",method="put",force_bodyargs=True),
        "dataplugin/register_url" : EndpointDefinition(name="register_url",method="post",force_bodyargs=True),
        "dataplugin/unregister_url" : EndpointDefinition(name="unregister_url",method="delete",force_bodyargs=True),
        "dataplugin" : [EndpointDefinition(name="dump_plugin",method="put",suffix="dump")],
        "jsondiff" : EndpointDefinition(name="jsondiff",method="post",force_bodyargs=True),
        "mapping/validate" : EndpointDefinition(name="validate_mapping",method="post",force_bodyargs=True),
        "buildconf" : [EndpointDefinition(name="create_build_conf",method="post",force_bodyargs=True),
                       EndpointDefinition(name="delete_build_conf",method="delete",force_bodyargs=True)],
        "index" : EndpointDefinition(name="index",method="put",force_bodyargs=True),
        "sync" : EndpointDefinition(name="sync",method="post",force_bodyargs=True),
        "whatsnew" : EndpointDefinition(name="whatsnew",method="get"),
        "status" : EndpointDefinition(name="status",method="get"),
        "api" : [EndpointDefinition(name="start_api",method="put",suffix="start"),
                 EndpointDefinition(name="stop_api",method="put",suffix="stop"),
                 EndpointDefinition(name="delete_api",method="delete",force_bodyargs=True),
                 EndpointDefinition(name="create_api",method="post",force_bodyargs=True)],
        "api/list" : EndpointDefinition(name="get_apis",method="get"),
        "stop" : EndpointDefinition(name="stop",method="put"),
        "restart" : EndpointDefinition(name="restart",method="put"),
        }

shell.set_commands(COMMANDS,EXTRA_NS)

import tornado.platform.asyncio
tornado.platform.asyncio.AsyncIOMainLoop().install()

settings = {'debug': True}
routes = generate_api_routes(shell, API_ENDPOINTS,settings=settings)
# add websocket endpoint
import biothings.hub.api.handlers.ws as ws
import sockjs.tornado
from biothings.utils.hub_db import ChangeWatcher
listener = ws.HubDBListener()
ChangeWatcher.add(listener)
ChangeWatcher.publish()
ws_router = sockjs.tornado.SockJSRouter(partial(ws.WebSocketConnection,listener=listener), '/ws')
routes.extend(ws_router.urls)

app = tornado.web.Application(routes,settings=settings)
EXTRA_NS["app"] = app

# register app into current event loop
app_server = tornado.httpserver.HTTPServer(app)
app_server.listen(config.HUB_API_PORT)
app_server.start()

server = start_server(loop,"MyVariant hub",passwords=config.HUB_PASSWD,
                      port=config.HUB_SSH_PORT,shell=shell)

try:
    loop.run_until_complete(server)
except (OSError, asyncssh.Error) as exc:
    sys.exit('Error starting server: ' + str(exc))

loop.run_forever()

