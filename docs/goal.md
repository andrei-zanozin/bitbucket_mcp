# Goal
Create a minimalist MCP server for Bitbucket with a toolset limited to necessary operations. By necessary operations, I mean those required for a team lead's daily work with Bitbucket, such as code reviews and browsing pull request history.

# List of actions required for the Bitbucket MCP server
This document describes the actions that must be supported by the MCP server. Handling multiple actions in a single MCP tool is preferable if it does not compromise the server architecture or make it more complex.

## Read actions
* Search for PRs in the repository by text.
* Read a PR's source and target branches.
* Read a PR's description.
* Read a PR's diff.
* Read a PR's discussions (comments).

## Create actions
* Create a PR.
* Create a discussion with an anchor.
* Create a general discussion.

## Update actions
* Resolve or unresolve a discussion.
* Update a discussion (post new replies).
* Update a PR's status.
