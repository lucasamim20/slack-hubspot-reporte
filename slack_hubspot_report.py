#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import io
import re
import json
import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from PIL import Image, ImageDraw, ImageFont

# =================== CONFIG ===================

TEMPLATE_PATH = os.getenv("REPORT_TEMPLATE_PATH", "report_template.png")

# Coordenadas (x, y) de cada campo no template
CELL_COORDS: Dict[str, tuple[int, int]] = {
    # Tickets (coluna "Turno atual")
    "Novo": (330, 120),
    "Coletar Pendências": (330, 140),
    "Em tratativa": (330, 160),
    "Retorno": (330, 180),
    "2ª Tentativa": (330, 200),
    "3ª Tentativa": (330, 220),
    "Solicita Imagem": (330, 240),
    "Financeiro": (330, 260),
    "Proativos - CGS": (330, 280),
    "Erros automação": (330, 300),
    "Centro de Gestão de Serviço": (330, 320),
    "Ocorrência": (330, 340),
    "Logística": (330, 360),
    "Readequação": (330, 380),
    "Concluído": (330, 400),

    # Conversas (coluna "Turno atual")
    "Tudo aberto em conversas": (330, 420),
    "Não atribuído": (330, 440),
    "Última Resposta (cliente)": (330, 460),
    "Tempo máx. última resposta (min)": (330, 480),
}

# Estágios que queremos contar (pode manter "limpo"; emojis e traços diferentes serão normalizados)
STAGE_LABELS: List[str] = [
    "Novo",
    "Coletar Pendências",
    "Em tratativa",
    "Retorno",
    "2ª Tentativa",
    "3ª Tentativa",
    "Solicita Imagem",      # mesmo que no HubSpot tenha emoji, a normalização trata
    "Financeiro",
    "Proativos - CGS",
    "Erros automação",
    "Centro de Gestão de Serviço",
    "Ocorrência",
    "Logística",
    "Readequação",
    "Concluído",
]

# =================== UTILS ===================

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
    except Exception:
        return ImageFont.load_default()

def normalize_label(txt: str) -> str:
    """
    Normaliza labels para comparação:
    - troca –/— por -
    - remove emojis e símbolos não alfanuméricos (mantém letras acentuadas, dígitos, espaço e '-')
    - trim + lowercase
    """
    txt = txt.replace("–", "-").replace("—", "-")
    txt = re.sub(r"[^\w\sÀ-ÿ-]", "", txt)
    return txt.strip().lower()

# =================== HUBSPOT HELPERS ===================

def hs_get(url: str, token: str, params: Optional[dict] = None) -> dict:
    r = requests.get(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, params=params or {})
    r.raise_for_status()
    return r.json()

def hs_post(url: str, token: str, payload: dict) -> dict:
    r = requests.post(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, json=payload)
    r.raise_for_status()
    return r.json()

def get_pipeline_and_stages(token: str,
                            pipeline_name_env: Optional[str],
                            pipeline_id_env: Optional[str]) -> tuple[str, Dict[str, str]]:
    """
    Retorna (pipelineId, {label: stageId}) do pipeline de tickets.
    Se HUBSPOT_PIPELINE_ID for fornecido, usa ele; senão, tenta pelo HUBSPOT_PIPELINE_NAME (label exato).
    """
    data = hs_get("https://api.hubapi.com/crm/v3/pipelines/tickets", token)

    chosen = None
    if pipeline_id_env:
        for p in data.get("results", []):
            if p.get("id") == pipeline_id_env:
                chosen = p
                break
        if not chosen:
            raise RuntimeError(f"Pipeline ID {pipeline_id_env} não encontrado nos pipelines de tickets.")

    if not chosen and pipeline_name_env:
        wanted = pipeline_name_env.strip().lower()
        for p in data.get("results", []):
            if p.get("label", "").strip().lower() == wanted:
                chosen = p
                break
        if not chosen:
            raise RuntimeError(f"Pipeline '{pipeline_name_env}' não encontrado. "
                               f"Ajuste HUBSPOT_PIPELINE_NAME ou defina HUBSPOT_PIPELINE_ID.")

    if not chosen:
        raise RuntimeError("Defina HUBSPOT_PIPELINE_NAME ou HUBSPOT_PIPELINE_ID nos Secrets.")

    pipeline_id = chosen.get("id")
    stage_map: Dict[str, str] = {st.get("label"): st.get("id") for st in chosen.get("stages", [])}

    # Logs úteis
    print("== PIPELINE ESCOLHIDO ==")
    print(f"ID: {pipeline_id}  LABEL: {chosen.get('label')}")
    print("== ESTÁGIOS DISPONÍVEIS ==")
    for lbl, sid in stage_map.items():
        print(f"- {lbl} -> {sid}")

    return pipeline_id, stage_map

def count_tickets_in_stage(token: str, pipeline_id: str, stage_id: str) -> int:
    """
    Conta tickets num estágio específico usando a Search API.
    Usa as propriedades corretas: hs_pipeline e hs_pipeline_stage.
    """
    url = "https://api.hubapi.com/crm/v3/objects/tickets/search"
    total = 0
    after = None

    while True:
        payload = {
            "filterGroups": [{
                "filters": [
                    {"propertyName": "hs_pipeline", "operator": "EQ", "value": pipeline_id},
                    {"propertyName": "hs_pipeline_stage", "operator": "EQ", "value": stage_id},
                ]
            }],
            "limit": 100,
            "properties": ["hs_pipeline", "hs_pipeline_stage"],
        }
        if after:
            payload["after"] = after

        try:
            data = hs_post(url, token, payload)
        except requests.HTTPError as e:
            try:
                print("Search payload:", payload)
                print("Search error body:", e.response.text)
            except Exception:
                pass
            raise

        total += len(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    return total

def fetch_ticket_metrics(token: str,
                         pipeline_name_default: Optional[str],
                         stage_labels: List[str]) -> Dict[str, int]:
    pipeline_id, stage_map = get_pipeline_and_stages(
        token,
        pipeline_name_env=os.getenv("HUBSPOT_PIPELINE_NAME", pipeline_name_default or ""),
        pipeline_id_env=os.getenv("HUBSPOT_PIPELINE_ID"),
    )

    # mapa normalizado: "solicita imagem" -> "<stage_id>"
    norm_stage_map: Dict[str, str] = {normalize_label(k): v for k, v in stage_map.items()}

    metrics: Dict[str, int] = {}
    for label in stage_labels:
        key = normalize_label(label)
        stage_id = norm_stage_map.get(key)
        if not stage_id:
            print(f"[WARN] Estágio não encontrado: '{label}' (normalizado='{key}'). "
                  f"Disponíveis: {list(norm_stage_map.keys())}")
            metrics[label] = 0
            continue

        metrics[label] = count_tickets_in_stage(token, pipeline_id, stage_id)

    return metrics

# =================== CONVERSAS (Inbox) ===================

def fetch_conversation_metrics(token: str, inbox_id: Optional[str] = None) -> Dict[str, int]:
    """
    Placeholders seguros. Para ligar de verdade:
      - habilite escopo conversations.read no Private App
      - consulte /conversations/v3/threads filtrando por status/assigned/lastMessage
    """
    return {
        "Tudo aberto em conversas": 0,
        "Não atribuído": 0,
        "Última Resposta (cliente)": 0,
        "Tempo máx. última resposta (min)": 0,
    }

# =================== IMAGEM ===================

def render_image(metrics: Dict[str, int], date_label: str) -> bytes:
    img = Image.open(TEMPLATE_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)
    font = _load_font(16)

    # Data no topo direito
    draw.text((img.width - 160, 10), date_label, font=font, fill=(0, 0, 0, 255))

    # Rótulos auxiliares (debug)
    show_keys = os.getenv("SHOW_KEYS", "0") == "1"
    key_font = _load_font(10)

    for key, value in metrics.items():
        if key not in CELL_COORDS:
            continue
        x, y = CELL_COORDS[key]
        draw.text((x, y), str(value), font=font, fill=(0, 0, 0, 255))
        if show_keys:
            draw.text((x + 28, y - 12), key, font=key_font, fill=(60, 60, 60, 255))

    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out.read()

# =================== MENSAGEM ===================

def build_message(slots_lines: str) -> str:
    template = """Bom dia, timaõzão!
Bora de reporte deste turno que se encerra.
Iniciamos com a fila completamente controlada, permaneceu assim durante todo turno.
Passei por todas as filas principais de tratativas internas (Novo, Col Pendências, Em Tratativa, Retornos 1/2/3, Financeiro, ProCGS, Sol Img, Erros automação).
Em PROCGS optei por não agendar, apenas enviar mensagens, pois estamos com poucos slots nesses primeiros dias da semana.

Fica 1 retorno ao qual só podemos contatar após às 09h (pedi ao perfeito do @anderson.santana cuidar deste :topzeiraaaaa: )

Consegui antecipar poucas ocorrências,

Agenda / Pendências :calendário_espiral:
{SLOTS}

Lembretes :anotações:
Mapeamento de oportunidades na Central - Thread
Fluxo de compartilhamento de imagem/Drive - Thread
Alinhamento dos Slots - Thread

:atenção: IMPORTANTE
Lembrando que a agenda de segunda e terça-feira para o RJ ultrapassa os slots e será necessário reagendar 1 visita em cada dia, lembrando as prioridades pré-definidas. Thread
A próxima semana tem diminuição nos slots de SP - CANVAS

Eras isso meu povo, vamos que bora que essa semana agosto acaba!! Boa semana a todos :coração_verde:
"""
    return template.replace("{SLOTS}", slots_lines)

# =================== SLACK ===================

def post_to_slack(token: str, channel: str, text: str, image_bytes: bytes, date_label: str):
    client = WebClient(token=token)
    try:
        upload = client.files_upload_v2(
            channel=channel,
            filename=f"reporte_operacional_{date_label}.png",
            file=image_bytes,
            title=f"Reporte Operacional - {date_label}",
            initial_comment=text,
        )
        return upload
    except SlackApiError as e:
        raise RuntimeError(f"Erro ao enviar para Slack: {e.response.get('error')}") from e

# =================== MAIN ===================

def main():
    parser = argparse.ArgumentParser(description="Gera e posta o reporte operacional no Slack.")
    parser.add_argument("--date", default="hoje", help="Data do reporte (YYYY-MM-DD) ou 'hoje'/'ontem'.")
    args = parser.parse_args()

    load_dotenv()

    tz = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/Sao_Paulo"))
    now_local = datetime.now(tz)

    if args.date.lower() == "hoje":
        report_date = now_local
    elif args.date.lower() == "ontem":
        report_date = now_local - timedelta(days=1)
    else:
        # aceita YYYY-MM-DD
        report_date = datetime.fromisoformat(args.date).replace(tzinfo=tz)

    date_label = report_date.strftime("%d/%m/%Y (%a)")

    slack_token = os.getenv("SLACK_BOT_TOKEN", "")
    slack_channel = os.getenv("SLACK_CHANNEL_ID", "")
    if not slack_token or not slack_channel:
        raise SystemExit("Preencha SLACK_BOT_TOKEN e SLACK_CHANNEL_ID (env/Secrets).")

    hs_token = os.getenv("HUBSPOT_TOKEN", "")
    if not hs_token:
        raise SystemExit("Preencha HUBSPOT_TOKEN (env/Secrets).")

    pipeline_name = os.getenv("HUBSPOT_PIPELINE_NAME", "Experiência do Cliente")

    # Tickets
    try:
        ticket_metrics = fetch_ticket_metrics(hs_token, pipeline_name, STAGE_LABELS)
    except Exception as e:
        print("Falha ao buscar tickets do HubSpot:", e)
        ticket_metrics = {label: 0 for label in STAGE_LABELS}

    # Conversas (opcional; 0 por padrão)
    conv_metrics = fetch_conversation_metrics(os.getenv("HUBSPOT_TOKEN", ""), os.getenv("HUBSPOT_INBOX_ID"))

    # Junta
    metrics: Dict[str, int] = {}
    metrics.update(ticket_metrics)
    metrics.update(conv_metrics)

    # Debug sem postar no Slack
    if os.getenv("DRY_RUN", "0") == "1":
        print("== MÉTRICAS ==")
        for k, v in metrics.items():
            print(f"{k}: {v}")
        return

    # Gera imagem
    image_bytes = render_image(metrics, date_label)

    # Slots (exemplo; troque por sua fonte real quando tiver)
    slots_lines = """25/08 - :sp: SP - 0 vagas
25/08 - :rj:  RJ - 0 vagas (1 excedente)
26/08 - :sp: SP - 3 vagas
26/08 - :rj:  RJ - 0 vagas (1 excedente)
27/08 - :sp: SP - 9 vagas
27/08 - :rj:  RJ - 8 vagas"""

    text = build_message(slots_lines)

    # Envia pro Slack
    resp = post_to_slack(slack_token, slack_channel, text, image_bytes, date_label)
    print(json.dumps({"ok": True, "file": resp.get("file", {}), "date": date_label}, ensure_ascii=False))

if __name__ == "__main__":
    main()
