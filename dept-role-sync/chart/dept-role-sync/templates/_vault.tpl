{{/*
Vault Agent Injector annotations. pre-populate-only: an init container
renders the secret and exits -- no long-running sidecar, which would
otherwise keep every Job pod from completing.

The secret is rendered as a JSON object of all its keys; the job loads it
via SECRETS_FILES. JSON (not KEY=VALUE) so passwords with any characters
survive intact.
*/}}
{{- define "dept-role-sync.vaultAnnotations" -}}
{{- $v := .Values.vault -}}
{{- $data := ternary ".Data.data" ".Data" (eq (int $v.kvVersion) 2) -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/agent-pre-populate-only: "true"
vault.hashicorp.com/role: {{ required "vault.role is required" $v.role | quote }}
vault.hashicorp.com/agent-inject-secret-credentials.json: {{ required "vault.secretPath is required" $v.secretPath | quote }}
vault.hashicorp.com/agent-inject-template-credentials.json: |
  {{ "{{-" }} with secret {{ $v.secretPath | quote }} {{ "-}}" }}{{ "{{" }} {{ $data }} | toJSON {{ "}}" }}{{ "{{-" }} end {{ "-}}" }}
{{- with $v.authPath }}
vault.hashicorp.com/auth-path: {{ . | quote }}
{{- end }}
{{- with $v.namespace }}
vault.hashicorp.com/namespace: {{ . | quote }}
{{- end }}
{{- with $v.address }}
vault.hashicorp.com/service: {{ . | quote }}
{{- end }}
{{- if $v.tlsSkipVerify }}
vault.hashicorp.com/tls-skip-verify: "true"
{{- end }}
{{- with $v.caCertSecret }}
vault.hashicorp.com/tls-secret: {{ . | quote }}
{{- end }}
{{- with $v.caCertPath }}
vault.hashicorp.com/ca-cert: {{ . | quote }}
{{- end }}
{{- with $v.annotations }}
{{ toYaml . }}
{{- end }}
{{- end }}
