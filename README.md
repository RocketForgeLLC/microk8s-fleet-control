# MicroK8s Fleet Control

Portable Kubernetes node-maintenance platform. The controller runs in the
target cluster; host agents run as systemd services on cluster members.

## Deploy

1. Apply `namespace.yaml`, `settings.yaml`, `rbac.yaml`, `service.yaml`, and
   `deployment.yaml` to the target cluster.
2. Set `maintenance-settings.data.apiServerEndpoint` to an API-server address
   reachable from cluster-member hosts. This is a deployment setting, not a
   hardcoded platform value.
3. Create the `maintenance-app` ConfigMap from the controller, UI, installer,
   and agent files, then restart the controller deployment.
4. Protect the controller with the administrator's chosen identity provider
   and expose the service through the administrator's chosen private ingress
   or tunnel.
5. In `/onboarding`, generate a short-lived plan code and run the displayed
   foreground installer on each node.

The installer creates a node certificate, enrolls the node, installs the
systemd agent, and establishes the outbound reporting channel. Node names,
addresses, storage policy, GPU taints, and update strategy are discovered from
the target cluster rather than embedded in the application.

Action execution remains disabled until the agent reports validated action
capability. Jobs are queued by the controller and delivered over the
authenticated outbound check-in channel; arbitrary shell commands are never
accepted.

## License

MicroK8s Fleet Control is available for private personal, educational, and
research use under the limited permission in [LICENSE.md](LICENSE.md).
Copying, forking, redistribution, modification, reuse, and commercial use
require separate written permission from the copyright holder. Permission
may be granted for a fee or at no charge.

