import json
import os
import traceback
from datetime import datetime, timedelta

from cronutils.error_handler import NullErrorHandler
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from forest.jasmine.traj2stats import gps_stats_main
from forest.willow.log_stats import log_stats_main
from pkg_resources import get_distribution

from api.data_access_api import chunk_fields
from config.constants import FOREST_QUEUE
from database.data_access_models import ChunkRegistry
from database.tableau_api_models import ForestTask
from libs.celery_control import forest_celery_app, safe_apply_async
from libs.forest_integration.constants import ForestTree
from libs.forest_integration.forest_data_interpretation import construct_summary_statistics


# run via cron every five minutes
from libs.s3 import s3_retrieve
from libs.streaming_zip import determine_file_name


TREE_TO_FOREST_FUNCTION = {
    ForestTree.jasmine: gps_stats_main,
    ForestTree.willow: log_stats_main,
}


def create_forest_celery_tasks():
    pending_trackers = ForestTask.objects.filter(status=ForestTask.Status.queued)

    # with make_error_sentry(sentry_type=SentryTypes.data_processing):  # add a new type?
    with NullErrorHandler():  # for debugging, does not suppress errors
        for tracker in pending_trackers:
            print(f"Queueing up celery task for {tracker.participant} on tree {tracker.forest_tree} from {tracker.data_date_start} to {tracker.data_date_end}")
            enqueue_forest_task(args=[tracker.id])


#run via celery as long as tasks exist
@forest_celery_app.task(queue=FOREST_QUEUE)
def celery_run_forest(forest_task_id):
    with transaction.atomic():
        tracker = ForestTask.objects.filter(id=forest_task_id).first()

        participant = tracker.participant
        forest_tree = tracker.forest_tree
        
        # Check if there already is a running task for this participant and tree, handling
        # concurrency and requeuing of the ask if necessary
        trackers = (
            ForestTask
                .objects
                .select_for_update()
                .filter(participant=participant, forest_tree=forest_tree)
        )
        if trackers.filter(status=ForestTask.Status.running).exists():
            enqueue_forest_task(args=[tracker.id])
            return
        
        # Get the chronologically earliest tracker that's queued
        tracker = (
            trackers
                .filter(status=ForestTask.Status.queued)
                .order_by("-data_date_start")
                .first()
        )
        if tracker is None:
            return
        
        # Set metadata on the tracker
        tracker.status = ForestTask.Status.running
        tracker.forest_version = get_distribution("forest").version
        tracker.process_start_time = timezone.now()
        tracker.save(update_fields=["status", "forest_version", "process_start_time"])

    # Save file size data
    chunks = ChunkRegistry.objects.filter(participant=participant)
    tracker.total_file_size = chunks.aggregate(Sum('file_size')).get('file_size__sum')
    tracker.save(update_fields=["total_file_size"])
    
    try:
        create_local_data_files(tracker, chunks)
        tracker.process_download_end_time = timezone.now()
        tracker.save(update_field=["process_download_end_time"])

        params_dict = tracker.params_dict()
        tracker.params_dict_cache = json.dumps(params_dict)
        tracker.save(update_field=["params_dict_cache"])
        TREE_TO_FOREST_FUNCTION[tracker.forest_tree](**params_dict)
        construct_summary_statistics(tracker)
        save_cached_files(tracker)
    
    except Exception:
        tracker.status = tracker.Status.error
        tracker.stacktrace = traceback.format_exc()
    else:
        tracker.status = tracker.Status.success
    tracker.process_end_time = timezone.now()
    tracker.save()
    
    tracker.clean_up_files()


def create_local_data_files(tracker, chunks):
    for chunk in chunks.values("study__object_id", *chunk_fields):
        contents = s3_retrieve(chunk["chunk_path"], chunk["study__object_id"], raw_path=True)
        file_name = os.path.join(
            tracker.data_input_path,
            determine_file_name(chunk),
        )
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        with open(file_name, "x") as f:
            f.write(contents.decode("utf-8"))


def enqueue_forest_task(**kwargs):
    updated_kwargs = {
        "expires": (datetime.utcnow() + timedelta(minutes=5)).replace(second=30, microsecond=0),
        "max_retries": 0,
        "retry": False,
        "task_publish_retry": False,
        "task_track_started": True,
        **kwargs,
    }
    safe_apply_async(celery_run_forest, **updated_kwargs)


def save_cached_files(tracker):
    if os.path.exists(tracker.all_bv_set_path):
        with open(tracker.all_bv_set_path, "rb") as f:
            tracker.save_all_bv_set_bytes(f.read())
    if os.path.exists(tracker.all_memory_dict_path):
        with open(tracker.all_memory_dict_path, "rb") as f:
            tracker.save_all_memory_dict_bytes(f.read())
