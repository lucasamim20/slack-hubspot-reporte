
# Slack + HubSpot Daily Report

Automatiza o envio diário do **Reporte Operacional** no Slack.

## Setup rápido
1) Python 3.10+ e `pip install -r requirements.txt`  
2) Copie `.env.example` para `.env` e preencha tokens/ID do canal  
3) Ajuste as coordenadas em `CELL_COORDS` conforme o seu template (se necessário)  
4) Teste:
```
python slack_hubspot_report.py --date hoje
```
Crontab de exemplo (8:01 todos os dias):
```
1 8 * * * /usr/bin/env bash -lc 'cd /caminho/auto_slack_report && /usr/bin/python3 slack_hubspot_report.py --date hoje >> cron.log 2>&1'
```


## HubSpot: Pipeline e estágios
Defina no `.env` (opcional, default já é **Experiência do Cliente**):
```
HUBSPOT_PIPELINE_NAME=Experiência do Cliente
```
O script descobre o `pipelineId` e o `stageId` de cada estágio pelo **label**, e conta os tickets com Search API.
Se algum estágio tiver nome diferente, ajuste a lista `stage_labels` no script.
