# MicroK8s Fleet Control Roadmap

This roadmap describes the planned capabilities for a generic, self-hosted
MicroK8s fleet management platform.

## Core platform

- [x] Run the controller inside the target Kubernetes cluster.
- [x] Discover cluster nodes from the Kubernetes API.
- [x] Provide a short-lived onboarding code and generated installer.
- [x] Enroll host agents with node certificates.
- [x] Persist platform settings, node reports, and queued jobs in Kubernetes.
- [x] Keep node-specific addresses, taints, pools, and storage policies
  discovered rather than hardcoded.

## Agent communication and security

- [x] Use an authenticated outbound agent check-in channel.
- [x] Validate agent identity before allowing maintenance actions.
- [x] Support agent version and update-channel reporting.
- [ ] Publish signed agent releases and support controlled self-update.
- [ ] Add certificate rotation and revocation workflows.
- [ ] Add configurable administrator roles and audit events.

## Fleet status and observability

- [x] Show Kubernetes readiness separately from agent communication status.
- [x] Distinguish never checked in, unavailable, and actively communicating.
- [x] Show agent version, channel, maintenance phase, and pending work.
- [x] Refresh the dashboard automatically from live status.
- [ ] Persist complete job logs in the controller.
- [ ] Add log search, download, retention, and per-job detail views.
- [ ] Add health history and maintenance timelines.

## Safe maintenance actions

- [x] Queue APT update, APT upgrade, and MicroK8s upgrade actions.
- [x] Require a fresh, validated agent before action execution.
- [x] Detect taints and special workload nodes automatically.
- [x] Check peer readiness and schedulability before protected maintenance.
- [x] Provide configurable Longhorn protection before drain.
- [ ] Run APT update immediately before APT upgrade.
- [ ] Restore workload replicas and scheduling state after maintenance.
- [ ] Improve handling for PodDisruptionBudgets and storage engine pods.
- [ ] Add maintenance cancellation and recovery actions.

## Upgrade planning

- [ ] Build a dynamic environment upgrade plan from current node state.
- [ ] Include only actions that are actually needed.
- [ ] Provide per-node and per-operation checkboxes.
- [ ] Support plans that select APT update, APT upgrade, or MicroK8s upgrade
  independently.
- [ ] Show the expected drain, scale-down, reboot, and restore sequence.
- [ ] Require explicit confirmation before executing a complete plan.
- [ ] Track plan progress and provide resumable execution.

## Configuration and user experience

- [ ] Add clear settings descriptions and help bubbles.
- [ ] Explain automatic taint detection, workload scale-down, and upgrade
  rejection behavior without cluster-specific wording.
- [ ] Return to the dashboard after saving settings.
- [ ] Link onboarding from settings and the dashboard.
- [ ] Expose configurable polling, log retention, update channels, and safety
  gates.
- [ ] Add a first-run readiness checklist.

## Deployment and portability

- [ ] Provide a complete generic Kubernetes deployment bundle.
- [ ] Document private ingress, tunnel, and identity-provider options.
- [ ] Support Entra ID and other OIDC providers without vendor lock-in.
- [ ] Document GPU-tainted nodes, shared storage, and Longhorn integration.
- [ ] Add upgrade and rollback guidance for the controller itself.
- [ ] Add automated validation for manifests, agent enrollment, and safe
  maintenance workflows.


