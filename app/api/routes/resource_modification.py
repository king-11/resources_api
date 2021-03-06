from os import environ

from algoliasearch.exceptions import AlgoliaException, AlgoliaUnreachableHostException
from flask import redirect, request, g
from sqlalchemy.exc import IntegrityError

from app import db, index, utils as utils
from app.api import bp
from app.api.auth import authenticate
from app.api.routes.helpers import (
    failures_counter, get_attributes, latency_summary, logger, ensure_bool)
from app.api.validations import requires_body, validate_resource, wrong_type
from app.models import Resource, VoteInformation, Key
import json as json_module


@latency_summary.time()
@failures_counter.count_exceptions()
@bp.route('/resources/<int:id>', methods=['PUT'], endpoint='update_resource')
@requires_body
@authenticate
def put_resource(id):
    json = request.get_json()

    if not isinstance(json, dict):
        return wrong_type("resource object", type(json))

    validation_errors = validate_resource(request.method, json, id)

    if validation_errors:
        errors = {"errors": validation_errors}
        return utils.standardize_response(payload=errors, status_code=422)
    return update_resource(id, request.get_json(), db)


def update_resource(id, json, db):
    resource = Resource.query.get(id)
    api_key = g.auth_key.apikey

    if not resource:
        return redirect('/404')

    langs, categ = get_attributes(json)
    index_object = {'objectID': id}

    def get_unique_resource_categories_as_strings():
        resources = Resource.query.all()
        return {resource.category.name for resource in resources}

    def get_unique_resource_languages_as_strings():
        resources = Resource.query.all()
        return {language.name
                for resource in resources
                for language in resource.languages}

    try:
        logger.info(
            f"Updating resource. Old data: "
            f"{json_module.dumps(resource.serialize(api_key))}")
        if json.get('languages') is not None:
            old_languages = resource.languages[:]
            resource.languages = langs
            index_object['languages'] = resource.serialize(api_key)['languages']
            resource_languages = get_unique_resource_languages_as_strings()
            for language in old_languages:
                if language.name not in resource_languages:
                    db.session.delete(language)
        if json.get('category'):
            old_category = resource.category
            resource.category = categ
            index_object['category'] = categ.name
            resource_categories = get_unique_resource_categories_as_strings()
            if old_category.name not in resource_categories:
                db.session.delete(old_category)
        if json.get('name'):
            resource.name = json.get('name')
            index_object['name'] = json.get('name')
        if json.get('url'):
            resource.url = json.get('url')
            index_object['url'] = json.get('url')
        if 'free' in json:
            free = ensure_bool(json.get('free'))
            resource.free = free
            index_object['free'] = free
        if 'notes' in json:
            resource.notes = json.get('notes')
            index_object['notes'] = json.get('notes')

        try:
            index.partial_update_object(index_object)

        except (AlgoliaUnreachableHostException, AlgoliaException) as e:
            if environ.get("FLASK_ENV") != 'development':
                logger.exception(e)
                msg = f"Algolia failed to update index for resource '{resource.name}'"
                logger.warn(msg)
                error = {'errors': [{"algolia-failed": {"message": msg}}]}
                return utils.standardize_response(payload=error, status_code=500)

        # Wait to commit the changes until we know that Aloglia was updated
        db.session.commit()

        return utils.standardize_response(
            payload=dict(
                data=resource.serialize(api_key)
            ),
            datatype="resource"
        )

    except IntegrityError as e:
        logger.exception(e)
        return utils.standardize_response(status_code=422)

    except Exception as e:
        logger.exception(e)
        return utils.standardize_response(status_code=500)


@latency_summary.time()
@failures_counter.count_exceptions()
@bp.route('/resources/<int:id>/<string:vote_direction>', methods=['PUT'])
@authenticate
def change_votes(id, vote_direction):
    return update_votes(id, f"{vote_direction}s") \
        if vote_direction in ['upvote', 'downvote'] else redirect('/404')


@latency_summary.time()
@failures_counter.count_exceptions()
@bp.route('/resources/<int:id>/click', methods=['PUT'])
@authenticate(allow_no_auth_key=True)
def update_resource_click(id):
    return add_click(id)


def update_votes(id, vote_direction_attribute):
    resource = Resource.query.get(id)

    if not resource:
        return redirect('/404')

    initial_count = getattr(resource, vote_direction_attribute)
    vote_direction = vote_direction_attribute[:-1]

    opposite_direction_attribute = 'downvotes' \
        if vote_direction_attribute == 'upvotes' else 'upvotes'
    opposite_direction = opposite_direction_attribute[:-1]
    opposite_count = getattr(resource, opposite_direction_attribute)

    api_key = g.auth_key.apikey
    vote_info = VoteInformation.query.get(
                {'voter_apikey': api_key, 'resource_id': id}
            )

    if vote_info is None:
        voter = Key.query.filter_by(apikey=api_key).first()
        new_vote_info = VoteInformation(
            voter_apikey=api_key,
            resource_id=resource.id,
            current_direction=vote_direction
        )
        new_vote_info.voter = voter
        resource.voters.append(new_vote_info)
        setattr(resource, vote_direction_attribute, initial_count + 1)
    else:
        if vote_info.current_direction == vote_direction:
            setattr(resource, vote_direction_attribute, initial_count - 1)
            setattr(vote_info, 'current_direction', None)
        else:
            setattr(resource, opposite_direction_attribute, opposite_count - 1) \
                if vote_info.current_direction == opposite_direction else None
            setattr(resource, vote_direction_attribute, initial_count + 1)
            setattr(vote_info, 'current_direction', vote_direction)
    db.session.commit()

    return utils.standardize_response(
        payload=dict(data=resource.serialize(api_key)),
        datatype="resource"
    )


def add_click(id):
    resource = Resource.query.get(id)
    api_key = g.auth_key.apikey if g.auth_key else None

    if not resource:
        return redirect('/404')

    initial_count = getattr(resource, 'times_clicked')
    setattr(resource, 'times_clicked', initial_count + 1)
    db.session.commit()

    return utils.standardize_response(
        payload=dict(data=resource.serialize(api_key)),
        datatype="resource")
