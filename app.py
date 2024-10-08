## Import the needed libraries
import json
import time
from datetime import datetime
from typing import Any, Optional
import requests
from decouple import config
from loguru import logger
from requests.auth import HTTPBasicAuth

# Get environment variables using the config object or os.environ["KEY"]
# These are the credentials passed by the variables of your pipeline to your tasks and in to your env

PORT_CLIENT_ID = config("PORT_CLIENT_ID")
PORT_CLIENT_SECRET = config("PORT_CLIENT_SECRET")
BITBUCKET_USERNAME = config("BITBUCKET_USERNAME")
BITBUCKET_PASSWORD = config("BITBUCKET_PASSWORD")
BITBUCKET_API_URL = config("BITBUCKET_HOST")
BITBUCKET_PROJECTS_FILTER = config(
    "BITBUCKET_PROJECTS_FILTER", cast=lambda v: v.split(",") if v else None, default=[]
)
PORT_API_URL =  config("PORT_API_URL", default="https://api.getport.io/v1")
WEBHOOK_SECRET = config("WEBHOOK_SECRET", default="bitbucket_webhook_secret")

## According to https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/
RATE_LIMIT = 1000  # Maximum number of requests allowed per hour
RATE_PERIOD = 3600  # Rate limit reset period in seconds (1 hour)
WEBHOOK_IDENTIFIER = "bitbucket_mapper"
WEBHOOK_EVENTS = [
    "repo:modified",
    "project:modified",
    "pr:modified",
    "pr:opened",
    "pr:merged",
    "pr:reviewer:updated",
    "pr:declined",
    "pr:deleted",
    "pr:comment:deleted",
    "pr:from_ref_updated",
    "pr:comment:edited",
    "pr:reviewer:unapproved",
    "pr:reviewer:needs_work",
    "pr:reviewer:approved",
    "pr:comment:added",
]

# Initialize rate limiting variables
request_count = 0
rate_limit_start = time.time()

## Get Port Access Token
credentials = {"clientId": PORT_CLIENT_ID, "clientSecret": PORT_CLIENT_SECRET}
token_response = requests.post(f"{PORT_API_URL}/auth/access_token", json=credentials)
access_token = token_response.json()["accessToken"]

# You can now use the value in access_token when making further requests
port_headers = {"Authorization": f"Bearer {access_token}"}

## Bitbucket user password https://developer.atlassian.com/server/bitbucket/how-tos/example-basic-authentication/
bitbucket_auth = HTTPBasicAuth(username=BITBUCKET_USERNAME, password=BITBUCKET_PASSWORD)


def get_or_create_port_webhook():
    logger.info("Checking if a Bitbucket webhook is configured on Port...")
    try:
        response = requests.get(
            f"{PORT_API_URL}/webhooks/{WEBHOOK_IDENTIFIER}",
            headers=port_headers,
        )
        response.raise_for_status()
        webhook_url = response.json().get("integration", {}).get("url")
        logger.info(f"Webhook configuration exists in port. URL: {webhook_url}")
        return webhook_url
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.info("Port webhook not found, creating a new one.")
            return create_port_webhook()
        else:
            logger.error(f"Error checking Port webhook: {e.response.status_code}")
            return None


def create_port_webhook():
    logger.info("Creating a webhook for bitbucket on Port...")
    with open("./resources/webhook_configuration.json", "r") as file:
        mappings = json.load(file)
    webhook_data = {
        "identifier": WEBHOOK_IDENTIFIER,
        "title": "Bitbucket Webhook",
        "description": "Webhook for receiving Bitbucket events",
        "icon": "BitBucket",
        "mappings": mappings,
        "enabled": True,
        "security": {
            "secret": WEBHOOK_SECRET,
            "signatureHeaderName": "X-Hub-Signature",
            "signatureAlgorithm": "sha256",
            "signaturePrefix": "sha256=",
            "requestIdentifierPath": ".headers['X-Request-ID']",
        },
        "integrationType": "custom",
    }

    try:
        response = requests.post(
            f"{PORT_API_URL}/webhooks",
            json=webhook_data,
            headers=port_headers,
        )
        response.raise_for_status()
        webhook_url = response.json().get("integration", {}).get("url")
        logger.info(
            f"Webhook configuration successfully created in Port: {webhook_url}"
        )
        return webhook_url
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 442:
            logger.error("Incorrect mapping, kindly fix!")
            return None
        logger.error(f"Error creating Port webhook: {e.response.status_code}")
        return None


def get_or_create_project_webhook(
    project_key: str, webhook_url: str, events: list[str]
):
    logger.info(f"Checking webhooks for project: {project_key}")
    if webhook_url is not None:
        try:
            matching_webhooks = [
                webhook
                for project_webhooks_batch in get_paginated_resource(
                    path=f"projects/{project_key}/webhooks"
                )
                for webhook in project_webhooks_batch
                if webhook["url"] == webhook_url
            ]
            if matching_webhooks:
                logger.info(f"Webhook already exists for project {project_key}")
                return matching_webhooks[0]
            logger.info(
                f"Webhook not found for project {project_key}. Creating a new one."
            )
            return create_project_webhook(
                project_key=project_key, webhook_url=webhook_url, events=events
            )
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"HTTP error when checking webhooks for project {project_key}: {e.response.status_code}"
            )
            return None
    else:
        logger.error("Port webhook URL is not available. Skipping webhook check...")
        return None


def create_project_webhook(project_key: str, webhook_url: str, events: list[str]):
    logger.info(f"Creating webhook for project: {project_key}")
    webhook_data = {
        "name": "Port Webhook",
        "url": webhook_url,
        "events": events,
        "active": True,
        "sslVerificationRequired": True,
        "configuration": {
            "secret": WEBHOOK_SECRET,
            "createdBy": "Port",
        },
    }
    try:
        response = requests.post(
            f"{BITBUCKET_API_URL}/rest/api/1.0/projects/{project_key}/webhooks",
            json=webhook_data,
            auth=bitbucket_auth,
        )
        response.raise_for_status()
        logger.info(f"Successfully created webhook for project {project_key}")
        return response.json()
    except requests.exceptions.HTTPError as e:
        logger.error(
            f"HTTP error when creating webhook for project {project_key}: {e.response.status_code}"
        )
        return None


def add_entity_to_port(blueprint_id, entity_object):
    response = requests.post(
        f"{PORT_API_URL}/blueprints/{blueprint_id}/entities?upsert=true&merge=true",
        json=entity_object,
        headers=port_headers,
    )
    logger.info(response.json())


def get_paginated_resource(
    path: str,
    params: dict[str, Any] = None,
    page_size: int = 25,
    full_response: bool = False,
):
    logger.info(f"Requesting data for {path}")

    global request_count, rate_limit_start

    # Check if we've exceeded the rate limit, and if so, wait until the reset period is over
    if request_count >= RATE_LIMIT:
        elapsed_time = time.time() - rate_limit_start
        if elapsed_time < RATE_PERIOD:
            sleep_time = RATE_PERIOD - elapsed_time
            time.sleep(sleep_time)

        # Reset the rate limiting variables
        request_count = 0
        rate_limit_start = time.time()

    url = f"{BITBUCKET_API_URL}/rest/api/1.0/{path}"
    params = params or {}
    params["limit"] = page_size
    next_page_start = None

    while True:
        try:
            if next_page_start:
                params["start"] = next_page_start

            response = requests.get(url=url, auth=bitbucket_auth, params=params)
            response.raise_for_status()
            page_json = response.json()
            request_count += 1

            if full_response:
                yield page_json
            else:
                batch_data = page_json["values"]
                yield batch_data

            # Check for next page start in response
            next_page_start = page_json.get("nextPageStart")

            # Break the loop if there is no more data
            if not next_page_start:
                break
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"HTTP error with code {e.response.status_code}, content: {e.response.text}"
            )
            if e.response.status_code == 404:
                logger.info(
                    f"Could not find the requested resources {path}. Terminating gracefully..."
                )
                return {}
            else:
                raise
    logger.info(f"Successfully fetched paginated data for {path}")


def get_single_project(project_key: str):
    response = requests.get(
        f"{BITBUCKET_API_URL}/rest/api/1.0/projects/{project_key}", auth=bitbucket_auth
    )
    response.raise_for_status()
    return response.json()


def convert_to_datetime(timestamp: int):
    converted_datetime = datetime.utcfromtimestamp(timestamp / 1000.0)
    return converted_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_repository_file_response(file_response: dict[str, Any]) -> str:
    lines = file_response.get("lines", [])
    logger.info(f"Received readme file with {len(lines)} entries")
    readme_content = ""

    for line in lines:
        readme_content += line.get("text", "") + "\n"

    return readme_content


def process_user_entities(users_data: list[dict[str, Any]]):
    blueprint_id = "bitbucketUser"

    for user in users_data:
        entity = {
            "identifier": user["emailAddress"],
            "title": user["displayName"],
            "properties": {
                "username": user["name"],
                "url": user["links"]["self"][0]["href"],
            },
            "relations": {},
        }
        add_entity_to_port(blueprint_id=blueprint_id, entity_object=entity)


def process_project_entities(projects_data: list[dict[str, Any]]):
    blueprint_id = "bitbucketProject"

    for project in projects_data:
        entity = {
            "identifier": project["key"],
            "title": project["name"],
            "properties": {
                "description": project.get("description"),
                "public": project["public"],
                "type": project["type"],
                "link": project["links"]["self"][0]["href"],
            },
            "relations": {},
        }
        add_entity_to_port(blueprint_id=blueprint_id, entity_object=entity)


def process_repository_entities(repository_data: list[dict[str, Any]]):
    blueprint_id = "bitbucketRepository"

    for repo in repository_data:
        readme_content = get_repository_readme(
            project_key=repo["project"]["key"], repo_slug=repo["slug"]
        )
        entity = {
            "identifier": repo["slug"],
            "title": repo["name"],
            "properties": {
                "description": repo.get("description"),
                "state": repo["state"],
                "forkable": repo["forkable"],
                "public": repo["public"],
                "link": repo["links"]["self"][0]["href"],
                "documentation": readme_content,
                "swagger_url": f"https://api.{repo['slug']}.com",
            },
            "relations": dict(
                project=repo["project"]["key"],
                latestCommitAuthor=repo.get("__latestCommit", {})
                .get("committer", {})
                .get("emailAddress"),
            ),
        }
        add_entity_to_port(blueprint_id=blueprint_id, entity_object=entity)


def process_pullrequest_entities(pullrequest_data: list[dict[str, Any]]):
    blueprint_id = "bitbucketPullrequest"

    for pr in pullrequest_data:
        entity = {
            "identifier": str(pr["id"]),
            "title": pr["title"],
            "properties": {
                "created_on": convert_to_datetime(pr["createdDate"]),
                "updated_on": convert_to_datetime(pr["updatedDate"]),
                "merge_commit": pr["fromRef"]["latestCommit"],
                "description": pr.get("description"),
                "state": pr["state"],
                "owner": pr["author"]["user"]["displayName"],
                "link": pr["links"]["self"][0]["href"],
                "destination": pr["toRef"]["displayId"],
                "reviewers": [
                    user["user"]["displayName"] for user in pr.get("reviewers", [])
                ],
                "source": pr["fromRef"]["displayId"],
            },
            "relations": {
                "repository": pr["toRef"]["repository"]["slug"],
                "participants": [pr.get("author")["user"]["emailAddress"]]
                + [user["user"]["emailAddress"] for user in pr.get("participants", [])],
            },
        }
        add_entity_to_port(blueprint_id=blueprint_id, entity_object=entity)


def get_repository_readme(project_key: str, repo_slug: str) -> str:
    file_path = f"projects/{project_key}/repos/{repo_slug}/browse/README.md"
    readme_content = ""
    for readme_file_batch in get_paginated_resource(
        path=file_path, page_size=500, full_response=True
    ):
        file_content = parse_repository_file_response(readme_file_batch)
        readme_content += file_content
    return readme_content


def get_latest_commit(project_key: str, repo_slug: str) -> dict[str, Any]:
    try:
        commit_path = f"projects/{project_key}/repos/{repo_slug}/commits"
        for commit_batch in get_paginated_resource(path=commit_path, page_size=1):
            if commit_batch:
                latest_commit = commit_batch[0]
                return latest_commit
    except Exception as e:
        logger.error(f"Error fetching latest commit for repo {repo_slug}: {e}")
    return {}


def get_repositories(project: dict[str, Any]):
    repositories_path = f"projects/{project['key']}/repos"
    for repositories_batch in get_paginated_resource(path=repositories_path):
        logger.info(
            f"received repositories batch with size {len(repositories_batch)} from project: {project['key']}"
        )
        process_repository_entities(
            repository_data=[
                {
                    **repo,
                    "__latestCommit": get_latest_commit(
                        project_key=project["key"], repo_slug=repo["slug"]
                    ),
                }
                for repo in repositories_batch
            ]
        )

        get_repository_pull_requests(repository_batch=repositories_batch)


def get_repository_pull_requests(repository_batch: list[dict[str, Any]]):
    pr_params = {"state": "ALL"}  ## Fetch all pull requests
    for repository in repository_batch:
        pull_requests_path = f"projects/{repository['project']['key']}/repos/{repository['slug']}/pull-requests"
        for pull_requests_batch in get_paginated_resource(
            path=pull_requests_path, params=pr_params
        ):
            logger.info(
                f"received pull requests batch with size {len(pull_requests_batch)} from repo: {repository['slug']}"
            )
            process_pullrequest_entities(pullrequest_data=pull_requests_batch)


if __name__ == "__main__":
    logger.info("Starting Bitbucket data extraction")
    for users_batch in get_paginated_resource(path="admin/users"):
        logger.info(f"received users batch with size {len(users_batch)}")
        process_user_entities(users_data=users_batch)
    project_path = "projects"
    if BITBUCKET_PROJECTS_FILTER:
        projects = (list(map(get_single_project, BITBUCKET_PROJECTS_FILTER)),)
    else:
        projects = get_paginated_resource(path=project_path)
    port_webhook_url = get_or_create_port_webhook()
    if not port_webhook_url:
        logger.error("Failed to get or create Port webhook. Skipping webhook setup...")
    for projects_batch in projects:
        logger.info(f"received projects batch with size {len(projects_batch)}")
        process_project_entities(projects_data=projects_batch)

        for project in projects_batch:
            get_repositories(project=project)
            webhooks = get_or_create_project_webhook(
                project_key=project["key"],
                webhook_url=port_webhook_url,
                events=WEBHOOK_EVENTS,
            )
    logger.info("Bitbucket data extraction completed")
