{{- define "dept-role-sync.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "dept-role-sync.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 52 | trimSuffix "-" }}
{{- else if contains (include "dept-role-sync.name" .) .Release.Name }}
{{- .Release.Name | trunc 52 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "dept-role-sync.name" .) | trunc 52 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "dept-role-sync.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{ include "dept-role-sync.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "dept-role-sync.selectorLabels" -}}
app.kubernetes.io/name: {{ include "dept-role-sync.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "dept-role-sync.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "dept-role-sync.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Name of the Secret holding credentials, "" when Vault supplies them. */}}
{{- define "dept-role-sync.secretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else if not .Values.vault.enabled }}
{{- include "dept-role-sync.fullname" . }}
{{- end }}
{{- end }}

{{/* CA bundle volume for one endpoint: args (dict "ca" .caBundle "name" volName) */}}
{{- define "dept-role-sync.caVolume" -}}
{{- if .ca.configMap }}
- name: {{ .name }}
  configMap:
    name: {{ .ca.configMap }}
    items: [{key: {{ .ca.key }}, path: ca.crt}]
{{- else if .ca.secret }}
- name: {{ .name }}
  secret:
    secretName: {{ .ca.secret }}
    items: [{key: {{ .ca.key }}, path: ca.crt}]
{{- end }}
{{- end }}
