"""Agente A2A v1.0 da Central de Salas (binding JSON-RPC sobre HTTP).

Por fora, um servidor A2A: Agent Card, `SendMessage` e `GetTask`. Por dentro, um
host MCP. Entre os dois fica a ponte: o `input_required` do MCP vira
`TASK_STATE_INPUT_REQUIRED`, e a resposta do cliente A2A vira o retry do
`tools/call`. O agente nao usa LLM e nao decide nada sobre salas: ele traduz protocolo.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import uvicorn
from mcp import types
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from host_mcp import ClienteMCP, HostMCP, novo_traceparent
from tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, Pausa, Tarefa, Tarefas, novo_id

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORTA", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://localhost:{PORTA}").rstrip("/")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

# A skill `reservar-sala` e atendida por esta tool. O agente so sabe o nome: a
# existencia e o schema de entrada vem do `tools/list`, a cada Task.
TOOL_DA_SKILL = "reservar_sala"

FORMATO_DO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
PEDIDO = re.compile(
    r"^reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+)$"
)
ESCOLHA = re.compile(r"^escolha=(?P<valor>\S+)$")
RECUSAR = "recusar"
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace_id>[0-9a-f]{32})-[0-9a-f]{16}-(?P<flags>[0-9a-f]{2})$")

CAMPOS_DO_ARTIFACT = ("reserva", "sala", "inicio", "fim", "responsavel")

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}

# Codigos de erro do A2A v1.0 no binding JSON-RPC.
TASK_NAO_ENCONTRADA = -32001
OPERACAO_NAO_SUPORTADA = -32004


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


tarefas = Tarefas()
host: HostMCP


def _linha_de_alternativas(pausa: Pausa) -> str:
    return "alternativas: " + ", ".join(pausa.alternativas)


def _traceparent(tarefa: Tarefa) -> str:
    return novo_traceparent(tarefa.trace_id, tarefa.trace_flags)


def _trace_do_cliente(request: Request) -> tuple[str, str]:
    """Trace-id e flags do header `traceparent` do cliente A2A. Sem header valido,
    o agente e a raiz do trace e sorteia um trace-id para a Task."""
    recebido = TRACEPARENT.match(request.headers.get("traceparent", "").strip().lower())
    if recebido and recebido["trace_id"] != "0" * 32:
        return recebido["trace_id"], recebido["flags"]
    return secrets.token_hex(16), "01"


def _texto_da_tool(resultado: types.CallToolResult) -> str:
    return " ".join(bloco.text for bloco in resultado.content if isinstance(bloco, types.TextContent))


def _pausa_de(resultado: types.InputRequiredResult) -> Pausa:
    """Le a elicitation do `input_required`: a chave atribuida pelo servidor, o campo
    pedido e as alternativas, na ordem do `enum`. O `requestState` so e guardado."""
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1:
        raise ValueError(f"esperava um unico inputRequest, vieram {len(pedidos)}")
    ((chave, pedido),) = pedidos.items()
    if not isinstance(pedido, types.ElicitRequest) or not isinstance(pedido.params, types.ElicitRequestFormParams):
        raise ValueError("o agente so atende elicitation em form mode")
    for campo, schema in (pedido.params.requested_schema.get("properties") or {}).items():
        alternativas = schema.get("enum") or ([schema["const"]] if "const" in schema else [])
        if alternativas:
            return Pausa(chave, campo, list(alternativas), resultado.request_state)
    raise ValueError("a elicitation nao oferece alternativas")


async def _chamar_tool(tarefa: Tarefa, mcp: ClienteMCP, respostas: types.InputResponses | None = None) -> None:
    """Uma rodada de `tools/call` e a traducao do resultado em estado da Task.

    Na primeira rodada `respostas` e None. No retry, o mesmo request e repetido
    com id JSON-RPC novo, levando `inputResponses` e o `requestState` guardado
    na pausa, ecoado sem modificacao.
    """
    pausa, tarefa.pausa = tarefa.pausa, None
    resultado = await mcp.chamar_tool(
        tarefa.ferramenta,
        tarefa.argumentos,
        _traceparent(tarefa),
        respostas=respostas,
        request_state=pausa.request_state if pausa else None,
    )
    if isinstance(resultado, types.InputRequiredResult):
        # A PONTE, ida: o `input_required` do MCP interrompe a Task do A2A.
        tarefa.pausa = _pausa_de(resultado)
        tarefa.mover(INPUT_REQUIRED, _linha_de_alternativas(tarefa.pausa))
        return
    if resultado.is_error:
        tarefa.mover(FAILED, _texto_da_tool(resultado))
        return
    dados = resultado.structured_content or {}
    if dados.get("reservado") is False:
        tarefa.mover(CANCELED, f"Reserva nao realizada: {dados.get('motivo')}.")
        return
    reserva = {campo: dados.get(campo) for campo in CAMPOS_DO_ARTIFACT}
    reserva["politica"] = tarefa.politica
    tarefa.artefatos.append({"artifactId": novo_id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva)}]})
    tarefa.mover(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


async def _descobrir_tool(tarefa: Tarefa, mcp: ClienteMCP) -> types.Tool:
    """`tools/list` antes de qualquer `tools/call`, em toda sessao. E dele que o SDK tira
    o outputSchema para validar o resultado: sem esta chamada ele listaria por conta
    propria, num request sem o `traceparent` da Task."""
    tools = await mcp.descobrir_tools(_traceparent(tarefa))
    tool = tools.get(TOOL_DA_SKILL)
    if tool is None:
        raise ValueError(f"a tool {TOOL_DA_SKILL} nao foi anunciada em tools/list")
    return tool


async def _preparar(tarefa: Tarefa, mcp: ClienteMCP, pedido: dict[str, str]) -> None:
    """Monta a chamada a partir do que foi descoberto e le a versao da politica."""
    tool = await _descobrir_tool(tarefa, mcp)
    # Os argumentos saem do inputSchema descoberto, nao de uma lista fixa no codigo.
    faltando = [campo for campo in tool.input_schema.get("required") or [] if campo not in pedido]
    if faltando:
        raise ValueError(f"a tool {TOOL_DA_SKILL} exige campos que o pedido nao traz: {', '.join(faltando)}")
    campos = tool.input_schema.get("properties") or {}
    tarefa.ferramenta = tool.name
    tarefa.argumentos = {campo: valor for campo, valor in pedido.items() if campo in campos}
    tarefa.politica = await mcp.ler_versao_da_politica(_traceparent(tarefa))


def _motivos(erro: BaseException) -> list[str]:
    if isinstance(erro, BaseExceptionGroup):
        return [motivo for interno in erro.exceptions for motivo in _motivos(interno)]
    return [str(erro) or type(erro).__name__]


async def _com_mcp(tarefa: Tarefa, passos: Callable[[ClienteMCP], Awaitable[None]]) -> None:
    """Roda `passos` com uma sessao MCP. Erro de protocolo (JSON-RPC `error`) ou de
    transporte nao tem como ser contornado pelo agente: a Task termina em FAILED."""
    try:
        async with host.sessao() as mcp:
            await passos(mcp)
    except Exception as erro:
        if tarefa.estado != WORKING:
            raise
        motivos = "; ".join(dict.fromkeys(_motivos(erro)))
        tarefa.mover(FAILED, f"Falha ao falar com o servidor MCP: {motivos}")


async def _abrir(mensagem: dict[str, Any], texto: str, request: Request) -> Tarefa:
    tarefa = tarefas.abrir(mensagem.get("contextId"), *_trace_do_cliente(request))
    tarefa.historico.append(mensagem)
    tarefa.mover(WORKING)

    pedido = PEDIDO.match(texto)
    if pedido is None:
        tarefa.mover(FAILED, f"Pedido invalido: use {FORMATO_DO_PEDIDO}")
        return tarefa

    async def passos(mcp: ClienteMCP) -> None:
        await _preparar(tarefa, mcp, pedido.groupdict())
        await _chamar_tool(tarefa, mcp)

    await _com_mcp(tarefa, passos)
    return tarefa


async def _continuar(tarefa: Tarefa, mensagem: dict[str, Any], texto: str) -> Tarefa:
    if tarefa.terminal:
        raise ErroA2A(
            OPERACAO_NAO_SUPORTADA,
            f"A Task {tarefa.id} esta em estado terminal ({tarefa.estado}) e nao aceita novas mensagens",
        )
    if tarefa.estado != INPUT_REQUIRED or tarefa.pausa is None:
        raise ErroA2A(OPERACAO_NAO_SUPORTADA, f"A Task {tarefa.id} nao esta aguardando entrada ({tarefa.estado})")
    if mensagem.get("contextId") not in (None, tarefa.contexto):
        raise ErroA2A(types.INVALID_PARAMS, f"contextId nao corresponde ao da Task {tarefa.id}")
    tarefa.historico.append(mensagem)

    pausa = tarefa.pausa
    escolha = ESCOLHA.match(texto)
    valor = escolha["valor"] if escolha else None
    if valor == RECUSAR:
        resposta = types.ElicitResult(action="decline")
    elif valor in pausa.alternativas:
        resposta = types.ElicitResult(action="accept", content={pausa.campo: valor})
    else:
        # Fora do enum: a Task continua interrompida e a pergunta e repetida.
        tarefa.mover(INPUT_REQUIRED, _linha_de_alternativas(pausa))
        return tarefa

    # A PONTE, volta: a resposta do cliente A2A retoma o `tools/call` original.
    tarefa.mover(WORKING)

    async def passos(mcp: ClienteMCP) -> None:
        await _descobrir_tool(tarefa, mcp)
        await _chamar_tool(tarefa, mcp, {pausa.chave: resposta})

    await _com_mcp(tarefa, passos)
    return tarefa


async def send_message(params: dict[str, Any], request: Request) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroA2A(types.INVALID_PARAMS, "params.message com parts e obrigatorio")
    texto = " ".join(p["text"] for p in mensagem["parts"] if isinstance(p, dict) and isinstance(p.get("text"), str))
    texto = texto.strip()

    task_id = mensagem.get("taskId")
    if task_id is None:
        tarefa = await _abrir(mensagem, texto, request)
    else:
        existente = tarefas.buscar(task_id) if isinstance(task_id, str) else None
        if existente is None:
            raise ErroA2A(TASK_NAO_ENCONTRADA, f"Task nao encontrada: {task_id}")
        tarefa = await _continuar(existente, mensagem, texto)
    return {"task": tarefa.publica()}


async def get_task(params: dict[str, Any], request: Request) -> dict[str, Any]:
    task_id = params.get("id")
    tarefa = tarefas.buscar(task_id) if isinstance(task_id, str) else None
    if tarefa is None:
        raise ErroA2A(TASK_NAO_ENCONTRADA, f"Task nao encontrada: {task_id}")
    tamanho = params.get("historyLength")
    return {"task": tarefa.publica(tamanho if isinstance(tamanho, int) else None)}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


def _erro(id: Any, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id, "error": {"code": codigo, "message": mensagem}})


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except ValueError:
        return _erro(None, types.PARSE_ERROR, "Parse error")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return _erro(None, types.INVALID_REQUEST, "Invalid Request")
    id = corpo.get("id")
    metodo = METODOS.get(corpo["method"])
    if metodo is None:
        return _erro(id, types.METHOD_NOT_FOUND, f"Metodo nao suportado: {corpo['method']}")
    params = corpo.get("params")
    if not isinstance(params, dict):
        return _erro(id, types.INVALID_PARAMS, "params deve ser um objeto")
    try:
        resultado = await metodo(params, request)
    except ErroA2A as erro:
        return _erro(id, erro.codigo, erro.mensagem)
    return JSONResponse({"jsonrpc": "2.0", "id": id, "result": resultado})


async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


@contextlib.asynccontextmanager
async def ciclo_de_vida(app: Starlette) -> AsyncIterator[None]:
    global host
    host = HostMCP(MCP_URL)
    try:
        yield
    finally:
        await host.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)

if __name__ == "__main__":
    print(f"[agente] A2A em {URL_PUBLICA}/a2a, servidor MCP em {MCP_URL}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
