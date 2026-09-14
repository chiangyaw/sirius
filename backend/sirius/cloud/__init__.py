"""
sirius-cloud — a standalone Model Context Protocol server for cloud auth + IaC.

Scoped today to AWS SSO login and Terraform, structured so Azure (`az`) and GCP
(`gcloud`) authentication can be added as sibling tools later. Like sirius-k8s it
runs over stdio with NO dependency on the Sirius event bus or app config, so it
works in any MCP client (Claude Code, etc.).

Run:  python -m sirius.cloud.main
"""
