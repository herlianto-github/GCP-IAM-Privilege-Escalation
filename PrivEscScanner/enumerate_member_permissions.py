#!/usr/bin/env python3

# ID of the current project (for Service Account checking)
PROJECT_ID = 'test-project2-233001'

import json
import google.oauth2.credentials
import googleapiclient
from googleapiclient import discovery
from googleapiclient import errors as google_api_errors


def get_project_ancestry(project_id, crm):
    response = crm.projects().getAncestry(projectId=project_id).execute()
    # This will include the project itself, so no need to manually take care of that
    return response['ancestor']


def get_iam_policies(project_ancestry, project_id, crm, crmv2):
    policies = {
        'Organizations': {},
        'Folders': {},
        'Projects': {}
    }
    body = {
        'options': {
            'requestedPolicyVersion': 3
        }
    }
    for resource in project_ancestry:
        try:
            if resource['resourceId']['type'] == 'project':
                # print(f'PROJECT: {resource["resourceId"]["id"]}')
                response = crm.projects().getIamPolicy(resource=project_id, body=body).execute()
                policies['Projects'][resource['resourceId']['id']] = response
            elif resource['resourceId']['type'] == 'folder':
                # print(f'FOLDER: {resource["resourceId"]["id"]}')
                response = crmv2.folders().getIamPolicy(resource=f'folders/{resource["resourceId"]["id"]}', body=body).execute()
                policies['Folders'][resource['resourceId']['id']] = response
            elif resource['resourceId']['type'] == 'organization':
                # print(f'ORGANIZATION: {resource["resourceId"]["id"]}')
                response = crm.organizations().getIamPolicy(resource=f'organizations/{resource["resourceId"]["id"]}', body=body).execute()
                policies['Organizations'][resource['resourceId']['id']] = response
            # print(json.dumps(response, indent=4))
        except google_api_errors.HttpError as error:
            if error.resp['status'] == '403' and 'The caller does not have permission' in str(error):
                print('ERROR: Missing permission to get IAM policy on a project/folder/org. Skipping related checks.')
            else:
                print(f'UNHANDLED ERROR IN get_iam_policies: {error}')

    return policies


def get_members_and_their_roles(policies):
    members = {
        'Organizations': {},
        'Folders': {},
        'Projects': {}
    }
    for resource_type in policies:
        for resource in policies[resource_type]:
            if not members[resource_type].get(resource):
                members[resource_type][resource] = {}
            bindings = policies[resource_type][resource].get('bindings', [])
            for binding in bindings:
                if binding.get('members'):
                    for member in binding['members']:
                        try:
                            members[resource_type][resource][member].append(binding['role'])
                        except KeyError:
                            members[resource_type][resource][member] = [binding['role']]
    return members


# credentials = None  # Application-Default
access_token = input('Enter an access token to use for authentication: ')
credentials = google.oauth2.credentials.Credentials(access_token.rstrip())
crm = discovery.build('cloudresourcemanager', 'v1', credentials=credentials)
# Only v2 has folders.getIamPolicy for some reason
crmv2 = discovery.build('cloudresourcemanager', 'v2', credentials=credentials)

project_ancestry = get_project_ancestry(PROJECT_ID, crm)
policies = get_iam_policies(project_ancestry, PROJECT_ID, crm, crmv2)

## Get all members and their roles from the org, folder, and project IAM policies
all_members = get_members_and_their_roles(policies)
# print(json.dumps(all_members, indent=2))

iam = discovery.build('iam', 'v1', credentials=credentials)

all_permissions = {
    'Organizations': {},
    'Folders': {},
    'Projects': {},
    'ServiceAccounts': {}
}

## Enumerate permissions for every role and associate them with each member
permissions_cache = {}  # Check to make sure this works, it didn't seem to save any time
# Make it so this doesn't call GCP for the permissions of the same role multiple times
for resource_type in all_members:
    for resource in all_members[resource_type]:
        if not all_permissions[resource_type].get(resource):
            all_permissions[resource_type][resource] = {}
        for member in all_members[resource_type][resource]:
            all_permissions[resource_type][resource][member] = []
            for role in all_members[resource_type][resource][member]:
                if permissions_cache.get(role):
                    role_perms = permissions_cache[role]
                else:
                    try:
                        res = iam.roles().get(name=role).execute()
                    except TypeError:
                        try:
                            res = iam.projects().roles().get(name=role).execute()
                        except googleapiclient.errors.HttpError:
                            res = {}
                    role_perms = res.get('includedPermissions', [])
                    permissions_cache[role] = role_perms
                all_permissions[resource_type][resource][member].extend(role_perms)

            all_permissions[resource_type][resource][member] = sorted(list(set(all_permissions[resource_type][resource][member])))

# Actual policy for each SA
service_account_policies = {}
# Actual permissions for each member
sa_member_permissions = {}

request = iam.projects().serviceAccounts().list(name=f'projects/{PROJECT_ID}')
while True:
    response = request.execute()

    for service_account in response.get('accounts', []):
        all_permissions['ServiceAccounts'][service_account['email']] = {}
        request = iam.projects().serviceAccounts().getIamPolicy(resource=service_account['name'], options_requestedPolicyVersion=3)
        response = request.execute()
        service_account_policies[service_account['email']] = response

    request = iam.projects().serviceAccounts().list_next(previous_request=request, previous_response=response)
    if request is None:
        break

for sa_email in service_account_policies:
    for binding in service_account_policies[sa_email].get('bindings', []):
        role = binding['role']
        for member in binding.get('members', []):
            if not all_permissions['ServiceAccounts'][sa_email].get(member):
                all_permissions['ServiceAccounts'][sa_email][member] = []
            if permissions_cache.get(role):
                role_perms = permissions_cache[role]
            else:
                try:
                    res = iam.roles().get(name=role).execute()
                except TypeError:
                    try:
                        res = iam.projects().roles().get(name=role).execute()
                    except googleapiclient.errors.HttpError:
                        res = {}
                role_perms = res.get('includedPermissions', [])
                permissions_cache[role] = role_perms
            all_permissions['ServiceAccounts'][sa_email][member].extend(role_perms)
            all_permissions['ServiceAccounts'][sa_email][member] = sorted(list(set(all_permissions['ServiceAccounts'][sa_email][member])))

with open('all_org_folder_proj_sa_permissions.json', 'w+') as f:
    json.dump(all_permissions, f, indent=4)

print('\nDone!')
print('Results were output to ./all_org_folder_proj_sa_permissions.json...')
