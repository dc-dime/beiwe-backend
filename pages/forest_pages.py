import datetime
from collections import defaultdict

from django.utils import timezone
from flask import render_template, request, abort, flash, redirect, Blueprint, url_for

from authentication.admin_authentication import authenticate_researcher_study_access, \
    researcher_is_an_admin, get_session_researcher, authenticate_admin, \
    get_researcher_allowed_studies
from database.data_access_models import ChunkRegistry
from database.study_models import Study
from database.tableau_api_models import ForestTracker
from database.user_models import Participant
from libs.forest_integration.constants import ForestTree
from libs.utils.date_utils import daterange


forest_pages = Blueprint('forest_pages', __name__)


@forest_pages.context_processor
def inject_html_params():
    # these variables will be accessible to every template rendering attached to the blueprint
    return {
        "allowed_studies": get_researcher_allowed_studies(),
        "is_admin": researcher_is_an_admin(),
    }


@forest_pages.route('/studies/<string:study_id>/forest/progress', methods=['GET'])
@authenticate_researcher_study_access
def analysis_progress(study_id=None):
    study = Study.objects.get(pk=study_id)
    participants = Participant.objects.filter(study=study_id)

    # generate chart of study analysis progress logs
    trackers = ForestTracker.objects.filter(participant__in=participants).order_by("created_on")

    try:
        start_date = ChunkRegistry.objects.filter(participant__in=participants).earliest("time_bin")
        end_date = ChunkRegistry.objects.filter(participant__in=participants).latest("time_bin")
        start_date = start_date.time_bin.date()
        end_date = end_date.time_bin.date()
    except ChunkRegistry.DoesNotExist:
        start_date = study.created_on.date()
        end_date = datetime.date.today()

    # this code simultaneously builds up the chart of most recent forest results for date ranges
    # by participant and tree, and tracks the metadata
    metadata = dict()
    results = defaultdict(lambda: "--")
    for tracker in trackers:
        for date in daterange(tracker.data_date_start, tracker.data_date_end, inclusive=True):
            results[(tracker.participant, tracker.forest_tree, date)] = tracker.status
            if tracker.status == tracker.Status.SUCCESS:
                metadata[(tracker.participant, tracker.forest_tree, date)] = tracker.metadata_id
            else:
                metadata[(tracker.participant, tracker.forest_tree, date)] = None

    # generate the date range for charting
    dates = list(daterange(start_date, end_date, inclusive=True))

    chart_columns = ["participant", "tree"] + dates
    chart = []

    for participant in participants:
        for tree in ForestTree.values():
            row = [participant.id, tree] + [results[(participant, tree, date)] for date in dates]
            chart.append(row)

    metadata_conflict = False
    # ensure that within each tree, only a single metadata value is used (only the most recent runs
    # are considered, and unsuccessful runs are assumed to invalidate old runs, clearing metadata)
    for tree in set([k[1] for k in metadata.keys()]):
        if len(set([m for k, m in metadata.items() if m is not None and k[1] == tree])) > 1:
            metadata_conflict = True
            break

    return render_template(
        'forest/analysis_progress.html',
        study=study,
        chart_columns=chart_columns,
        status_choices=ForestTracker.Status,
        metadata_conflict=metadata_conflict,
        start_date=start_date,
        end_date=end_date,
        chart=chart  # this uses the jinja safe filter and should never involve user input
    )


@forest_pages.route('/studies/<string:study_id>/forest/create-tasks', methods=['GET', 'POST'])
@authenticate_admin
def create_tasks(study_id=None):
    # Only a SITE admin ccran queue forest tasks
    if not get_session_researcher().site_admin:
        return abort(403)
    try:
        study = Study.objects.get(pk=study_id)
    except Study.DoesNotExist:
        return abort(404)
    
    participants = Participant.objects.filter(study=study_id)
    try:
        start_date = ChunkRegistry.objects.filter(participant__in=participants).earliest("time_bin")
        end_date = ChunkRegistry.objects.filter(participant__in=participants).latest("time_bin")
        start_date = start_date.time_bin.date()
        end_date = end_date.time_bin.date()
    except ChunkRegistry.DoesNotExist:
        start_date = study.created_on.date()
        end_date = timezone.now().date()
    
    if request.method == 'GET':
        return render_template(
            "forest/create_tasks.html",
            study=study.as_unpacked_native_python(),
            participants=list(
                study.participants.order_by("patient_id").values_list("patient_id", flat=True)
            ),
            trees=ForestTree.values(),
            start_date=start_date.strftime('%Y-%m-%d'),
            end_date=end_date.strftime('%Y-%m-%d')
        )
    
    start_date = datetime.datetime.strptime(request.form.get("date_start"), "%Y-%m-%d").date()
    end_date = datetime.datetime.strptime(request.form.get("date_end"), "%Y-%m-%d").date()
    
    for participant_id in request.form.getlist("user_ids"):
        for tree in request.form.getlist("trees"):
            participant = Participant.objects.get(patient_id=participant_id)
            ForestTracker(
                participant=participant,
                forest_tree=tree,
                data_date_start=start_date,
                data_date_end=end_date,
                status=ForestTracker.Status.QUEUED,
                metadata=study.forest_metadata,
            ).save()
            # TODO: add missing params or update model defaults
    flash("Forest tasks successfully queued!", "success")
    
    return redirect(url_for("forest_pages.task_log", study_id=study_id))


@forest_pages.route('/studies/<string:study_id>/forest/task-log', methods=['GET', 'POST'])
@authenticate_researcher_study_access
def task_log(study_id=None):
    study = Study.objects.get(pk=study_id)
    if request.method == 'GET':
        return render_template(
            'forest/task_log.html',
            study=study,
            is_site_admin=get_session_researcher().site_admin,
            status_choices=ForestTracker.Status,
            forest_log=list(ForestTracker.objects.all().order_by("-created_on").values("participant", "forest_tree", "data_date_start", "data_date_end", "stacktrace", "status", "external_id", "created_on"))
        )

    # post request is to cancel a forest task, requires site admin permissions
    if not get_session_researcher().site_admin:
        return abort(403)

    forest_task_id = request.values.get("task_id")
    if forest_task_id is None:
        return abort(404)
    number_updated = (
        ForestTracker
            .objects
            .filter(external_id=forest_task_id, status=ForestTracker.Status.QUEUED)
            .update(
                status=ForestTracker.Status.CANCELLED,
                stacktrace=f'Canceled by {get_session_researcher().username} on {datetime.date.today()}',
            )
    )
    if number_updated > 0:
        flash("Forest task successfully cancelled.", "success")
    else:
        flash("Sorry, we were unable to find or cancel this Forest task.", "warning")

    return redirect(url_for("forest_pages.task_log", study_id=study_id))

