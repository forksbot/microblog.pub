import json
import traceback
from datetime import datetime
from datetime import timezone

import flask
import requests
from flask import current_app as app
from little_boxes import activitypub as ap
from little_boxes.errors import ActivityGoneError
from little_boxes.errors import ActivityNotFoundError
from little_boxes.errors import NotAnActivityError
from little_boxes.httpsig import HTTPSigAuth
from requests.exceptions import HTTPError

import config
from config import DB
from core import gc
from core.activitypub import Box
from core.activitypub import _actor_hash
from core.activitypub import _add_answers_to_question
from core.activitypub import no_cache
from core.activitypub import post_to_outbox
from core.db import update_one_activity
from core.inbox import process_inbox
from core.meta import MetaKey
from core.meta import by_remote_id
from core.meta import flag
from core.meta import upsert
from core.notifications import set_inbox_flags
from core.outbox import process_outbox
from core.remote import track_failed_send
from core.remote import track_successful_send
from core.shared import MY_PERSON
from core.shared import _Response
from core.shared import back
from core.shared import p
from core.tasks import Tasks
from utils import now
from utils import opengraph

SIG_AUTH = HTTPSigAuth(config.KEY)

blueprint = flask.Blueprint("tasks", __name__)


class TaskError(Exception):
    """Raised to log the error for poussetaches."""

    def __init__(self):
        self.message = traceback.format_exc()


@blueprint.route("/task/update_question", methods=["POST"])
def task_update_question() -> _Response:
    """Sends an Update."""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        app.logger.info(f"Updating question {iri}")
        cc = [config.ID + "/followers"]
        doc = DB.activities.find_one({"box": Box.OUTBOX.value, "remote_id": iri})
        _add_answers_to_question(doc)
        question = ap.Question(**doc["activity"]["object"])

        raw_update = dict(
            actor=question.id,
            object=question.to_dict(embed=True),
            attributedTo=MY_PERSON.id,
            cc=list(set(cc)),
            to=[ap.AS_PUBLIC],
        )
        raw_update["@context"] = config.DEFAULT_CTX

        update = ap.Update(**raw_update)
        print(update)
        print(update.to_dict())
        post_to_outbox(update)

    except HTTPError as err:
        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@blueprint.route("/task/fetch_og_meta", methods=["POST"])
def task_fetch_og_meta() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")
        if activity.has_type(ap.ActivityType.CREATE):
            note = activity.get_object()
            links = opengraph.links_from_note(note.to_dict())
            og_metadata = opengraph.fetch_og_metadata(config.USER_AGENT, links)
            for og in og_metadata:
                if not og.get("image"):
                    continue
                config.MEDIA_CACHE.cache_og_image(og["image"], iri)

            app.logger.debug(f"OG metadata {og_metadata!r}")
            DB.activities.update_one(
                {"remote_id": iri}, {"$set": {"meta.og_metadata": og_metadata}}
            )

        app.logger.info(f"OG metadata fetched for {iri}: {og_metadata}")
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"dropping activity {iri}, skip OG metedata")
        return ""
    except requests.exceptions.HTTPError as http_err:
        if 400 <= http_err.response.status_code < 500:
            app.logger.exception("bad request, no retry")
            return ""
        app.logger.exception("failed to fetch OG metadata")
        raise TaskError() from http_err
    except Exception as err:
        app.logger.exception(f"failed to fetch OG metadata for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cache_object", methods=["POST"])
def task_cache_object() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")
        obj = activity.get_object()
        obj_actor = obj.get_actor()
        obj_actor_hash = _actor_hash(obj_actor)

        cache = {MetaKey.OBJECT: obj.to_dict(embed=True)}

        if activity.get_actor().id != obj_actor.id:
            cache[MetaKey.OBJECT_ACTOR] = obj_actor.to_dict(embed=True)
            cache[MetaKey.OBJECT_ACTOR_ID] = obj_actor.id
            cache[MetaKey.OBJECT_ACTOR_HASH] = obj_actor_hash

        # FIXME(tsileo): set OBJECT_ACTOR_HASH (like in "cache actor"  and do an update_many even for ACTOR (not only
        # OBJECT_ACTOR) ; a migration for OBJECT_ACTOR_ID/OBJECT_ACTOR_HASH needed?
        update_one_activity(by_remote_id(activity.id), upsert(cache))
    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        DB.activities.update_one({"remote_id": iri}, {"$set": {"meta.deleted": True}})
        app.logger.exception(f"flagging activity {iri} as deleted, no object caching")
    except Exception as err:
        app.logger.exception(f"failed to cache object for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/finish_post_to_outbox", methods=["POST"])  # noqa:C901
def task_finish_post_to_outbox() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        recipients = activity.recipients()

        process_outbox(activity, {})

        app.logger.info(f"recipients={recipients}")
        activity = ap.clean_activity(activity.to_dict())

        payload = json.dumps(activity)
        for recp in recipients:
            app.logger.debug(f"posting to {recp}")
            Tasks.post_to_remote_inbox(payload, recp)
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"no retry")
    except Exception as err:
        app.logger.exception(f"failed to post to remote inbox for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/finish_post_to_inbox", methods=["POST"])  # noqa: C901
def task_finish_post_to_inbox() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        process_inbox(activity, {})

    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        app.logger.exception(f"no retry")
    except Exception as err:
        app.logger.exception(f"failed to cfinish post to inbox for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cache_attachments", methods=["POST"])
def task_cache_attachments() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")
        # Generates thumbnails for the actor's icon and the attachments if any

        obj = activity.get_object()

        # Iter the attachments
        for attachment in obj._data.get("attachment", []):
            try:
                config.MEDIA_CACHE.cache_attachment(attachment, iri)
            except ValueError:
                app.logger.exception(f"failed to cache {attachment}")

        app.logger.info(f"attachments cached for {iri}")

    except (ActivityGoneError, ActivityNotFoundError, NotAnActivityError):
        app.logger.exception(f"dropping activity {iri}, no attachment caching")
    except Exception as err:
        app.logger.exception(f"failed to cache attachments for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cache_actor", methods=["POST"])
def task_cache_actor() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload["iri"]
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        # Reload the actor without caching (in case it got upated)
        with no_cache():
            actor = ap.fetch_remote_activity(activity.get_actor().id)

        actor_hash = _actor_hash(actor)

        # Fetch the Open Grah metadata if it's a `Create`
        if activity.has_type(ap.ActivityType.CREATE):
            Tasks.fetch_og_meta(iri)

        if actor.icon:
            if isinstance(actor.icon, dict) and "url" in actor.icon:
                config.MEDIA_CACHE.cache_actor_icon(actor.icon["url"])
            else:
                app.logger.warning(f"failed to parse icon {actor.icon} for {iri}")

        if activity.has_type(ap.ActivityType.FOLLOW):
            if actor.id == config.ID:
                # It's a new following, cache the "object" (which is the actor we follow)
                DB.activities.update_one(
                    by_remote_id(iri),
                    upsert({MetaKey.OBJECT: activity.get_object().to_dict(embed=True)}),
                )

        # Cache the actor info
        DB.activities.update_many(
            {
                **flag(MetaKey.ACTOR_ID, actor.id),
                **flag(MetaKey.ACTOR_HASH, {"$ne": actor_hash}),
            },
            upsert({MetaKey.ACTOR: actor.to_dict(embed=True)}),
        )

        # TODO(tsileo): Also update following (it's in the object)
        # DB.activities.update_many(
        #     {"meta.object_id": actor.id}, {"$set": {"meta.object": actor.to_dict(embed=True)}}
        # )

        app.logger.info(f"actor cached for {iri}")
        if activity.has_type([ap.ActivityType.CREATE, ap.ActivityType.ANNOUNCE]):
            Tasks.cache_attachments(iri)

    except (ActivityGoneError, ActivityNotFoundError):
        DB.activities.update_one({"remote_id": iri}, {"$set": {"meta.deleted": True}})
        app.logger.exception(f"flagging activity {iri} as deleted, no actor caching")
    except Exception as err:
        app.logger.exception(f"failed to cache actor for {iri}")
        raise TaskError() from err

    return ""


@blueprint.route("/task/forward_activity", methods=["POST"])
def task_forward_activity() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        recipients = back.followers_as_recipients()
        app.logger.debug(f"Forwarding {activity!r} to {recipients}")
        activity = ap.clean_activity(activity.to_dict())
        payload = json.dumps(activity)
        for recp in recipients:
            app.logger.debug(f"forwarding {activity!r} to {recp}")
            Tasks.post_to_remote_inbox(payload, recp)
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@blueprint.route("/task/post_to_remote_inbox", methods=["POST"])
def task_post_to_remote_inbox() -> _Response:
    """Post an activity to a remote inbox."""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    payload, to = task.payload["payload"], task.payload["to"]
    try:
        app.logger.info("payload=%s", payload)
        app.logger.info("generating sig")
        signed_payload = json.loads(payload)

        app.logger.info("to=%s", to)
        resp = requests.post(
            to,
            data=json.dumps(signed_payload),
            auth=SIG_AUTH,
            headers={
                "Content-Type": config.HEADERS[1],
                "Accept": config.HEADERS[1],
                "User-Agent": config.USER_AGENT,
            },
        )
        app.logger.info("resp=%s", resp)
        app.logger.info("resp_body=%s", resp.text)
        resp.raise_for_status()
    except HTTPError as err:
        track_failed_send(to)

        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except requests.RequestException:
        track_failed_send(to)

        app.logger.exception("request failed")

    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    track_successful_send(to)

    return ""


@blueprint.route("/task/fetch_remote_question", methods=["POST"])
def task_fetch_remote_question() -> _Response:
    """Fetch a remote question for implementation that does not send Update."""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        app.logger.info(f"Fetching remote question {iri}")
        local_question = DB.activities.find_one(
            {
                "box": Box.INBOX.value,
                "type": ap.ActivityType.CREATE.value,
                "activity.object.id": iri,
            }
        )
        remote_question = ap.get_backend().fetch_iri(iri, no_cache=True)
        # FIXME(tsileo): compute and set `meta.object_visiblity` (also update utils.py to do it)
        if (
            local_question
            and (
                local_question["meta"].get("voted_for")
                or local_question["meta"].get("subscribed")
            )
            and not DB.notifications.find_one({"activity.id": remote_question["id"]})
        ):
            DB.notifications.insert_one(
                {
                    "type": "question_ended",
                    "datetime": datetime.now(timezone.utc).isoformat(),
                    "activity": remote_question,
                }
            )

        # Update the Create if we received it in the inbox
        if local_question:
            DB.activities.update_one(
                {"remote_id": local_question["remote_id"], "box": Box.INBOX.value},
                {"$set": {"activity.object": remote_question}},
            )

        # Also update all the cached copies (Like, Announce...)
        DB.activities.update_many(
            {"meta.object.id": remote_question["id"]},
            {"$set": {"meta.object": remote_question}},
        )

    except HTTPError as err:
        app.logger.exception("request failed")
        if 400 >= err.response.status_code >= 499:
            app.logger.info("client error, no retry")
            return ""

        raise TaskError() from err
    except Exception as err:
        app.logger.exception("task failed")
        raise TaskError() from err

    return ""


@blueprint.route("/task/cleanup", methods=["POST"])
def task_cleanup() -> _Response:
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    gc.perform()
    return ""


@blueprint.route("/task/process_new_activity", methods=["POST"])  # noqa:c901
def task_process_new_activity() -> _Response:
    """Process an activity received in the inbox"""
    task = p.parse(flask.request)
    app.logger.info(f"task={task!r}")
    iri = task.payload
    try:
        activity = ap.fetch_remote_activity(iri)
        app.logger.info(f"activity={activity!r}")

        flags = {}

        if not activity.published:
            flags.update(flag(MetaKey.PUBLISHED, now()))
        else:
            flags.update(flag(MetaKey.PUBLISHED, activity.published))

        set_inbox_flags(activity, flags)
        app.logger.info(f"a={activity}, flags={flags!r}")
        if flags:
            DB.activities.update_one({"remote_id": activity.id}, {"$set": flags})

        app.logger.info(f"new activity {iri} processed")
        if not activity.has_type(ap.ActivityType.DELETE):
            Tasks.cache_actor(iri)
    except (ActivityGoneError, ActivityNotFoundError):
        app.logger.exception(f"dropping activity {iri}, skip processing")
        return ""
    except Exception as err:
        app.logger.exception(f"failed to process new activity {iri}")
        raise TaskError() from err

    return ""
