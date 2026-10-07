"""Servidor MCP da Central de Salas (Streamable HTTP, spec 2026-07-28).

Tres tools, um resource e o ciclo de MRTR em `reservar_sala`. O servidor nao tem
sessao nem memoria entre rodadas: quando precisa de uma escolha do usuario ele
termina a resposta com `input_required`, e o pedido inteiro viaja selado no
`requestState` ate o retry.
"""

from __future__ import annotations

import json
import os
import sys

import uvicorn
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    MissingRequiredClientCapabilityErrorData,
)
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from salas import ERRO_SEM_ALTERNATIVA, CentralDeSalas, ErroDeRegra

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORTA = int(os.environ.get("MCP_PORTA", "7301"))
VALIDADE_DO_REQUEST_STATE = 10 * 60  # segundos
CHAVE_DA_ESCOLHA = "escolha_de_sala"
MENSAGEM_DE_ESCOLHA = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


def _seguranca_do_request_state() -> RequestStateSecurity:
    """A chave vem so do ambiente: e ela que faz o `requestState` sobreviver a um restart."""
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere e exporte uma chave com:\n"
            "  export REQUEST_STATE_SECRET=\"$(python3 -c 'import secrets; print(secrets.token_hex(32))')\""
        )
    return RequestStateSecurity(keys=[segredo], ttl=VALIDADE_DO_REQUEST_STATE)


central = CentralDeSalas()
mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=_seguranca_do_request_state(),
)


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class PerguntaSelada(BaseModel):
    """O que viaja no `requestState`: o pedido original e as alternativas oferecidas.

    E tudo que o retry precisa. O servidor nao guarda nada entre as rodadas, entao
    a pergunta continua respondivel depois de um restart do processo.
    """

    sala: str
    inicio: str
    fim: str
    responsavel: str
    alternativas: list[str]


def _exigir_elicitation_em_formulario(ctx: Context) -> None:
    """Sem a capability declarada neste request, o servidor nao pergunta: responde -32021."""
    elicitation = ctx.client_capabilities.elicitation if ctx.client_capabilities else None
    if elicitation is not None and elicitation.form is not None:
        return
    exigido = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="O cliente nao declarou a capability de elicitation em form mode, exigida para escolher a sala",
        data=MissingRequiredClientCapabilityErrorData(required_capabilities=exigido).model_dump(
            by_alias=True, mode="json", exclude_none=True
        ),
    )


def _perguntar(ctx: Context, sala: str, inicio: str, fim: str, responsavel: str) -> InputRequiredResult:
    """Termina a resposta pedindo a escolha. Nao ha canal de volta: quem retorna e o cliente."""
    alternativas = central.alternativas(sala, central.validar(sala, inicio, fim))
    if not alternativas:
        raise ErroDeRegra(ERRO_SEM_ALTERNATIVA)
    _exigir_elicitation_em_formulario(ctx)
    formulario = {
        "type": "object",
        "properties": {
            "sala": {
                "type": "string",
                "title": "Sala",
                "description": "Sala alternativa escolhida",
                "enum": alternativas,
            }
        },
        "required": ["sala"],
    }
    selado = PerguntaSelada(sala=sala, inicio=inicio, fim=fim, responsavel=responsavel, alternativas=alternativas)
    return InputRequiredResult(
        input_requests={
            CHAVE_DA_ESCOLHA: ElicitRequest(
                params=ElicitRequestFormParams(message=MENSAGEM_DE_ESCOLHA, requested_schema=formulario)
            )
        },
        # Sai daqui em texto claro: o SDK sela (AES-GCM), carimba a expiracao e amarra
        # ao request antes de por no fio, e so devolve o que passar na verificacao.
        request_state=selado.model_dump_json(),
    )


def _reservar(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    reserva = central.reservar(sala, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva["id"],
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=central.versao_da_politica,
    )


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
async def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**sala) for sala in central.salas])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    conflitos = central.conflitos(sala, central.validar(sala, inicio, fim))
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[ConflitoOut(**{campo: r[campo] for campo in ConflitoOut.model_fields}) for r in conflitos],
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
async def reservar_sala(
    sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
) -> ReservaOut | InputRequiredResult:
    if ctx.request_state is None:
        # Primeira rodada: reserva direto, ou termina a resposta com a pergunta.
        if not central.conflitos(sala, central.validar(sala, inicio, fim)):
            return _reservar(sala, inicio, fim, responsavel)
        return _perguntar(ctx, sala, inicio, fim, responsavel)

    # Retry. O `requestState` ja chegou aqui verificado pelo SDK (adulterado ou
    # expirado nem entra na tool: -32602). O pedido e reconstruido do que foi
    # selado, e nao dos argumentos que o cliente reenviou.
    pedido = PerguntaSelada.model_validate_json(ctx.request_state)
    resposta = (ctx.input_responses or {}).get(CHAVE_DA_ESCOLHA)
    if not isinstance(resposta, ElicitResult):
        return _perguntar(ctx, pedido.sala, pedido.inicio, pedido.fim, pedido.responsavel)
    if resposta.action != "accept":
        return ReservaOut(reservado=False, motivo="recusado" if resposta.action == "decline" else "cancelado")
    escolhida = (resposta.content or {}).get("sala")
    if escolhida not in pedido.alternativas:
        raise ErroDeRegra(f"Escolha fora das alternativas oferecidas: {escolhida}")
    if central.conflitos(escolhida, central.validar(escolhida, pedido.inicio, pedido.fim)):
        # A alternativa foi ocupada entre a pergunta e a resposta: pergunta de novo.
        return _perguntar(ctx, pedido.sala, pedido.inicio, pedido.fim, pedido.responsavel)
    return _reservar(escolhida, pedido.inicio, pedido.fim, pedido.responsavel)


@mcp.resource(
    "politica://uso",
    name="politica-de-uso",
    description="Politica de uso das salas. A primeira linha declara a versao.",
    mime_type="text/markdown",
)
def politica_de_uso() -> str:
    return central.politica


class RegistroDeRequests:
    """Middleware ASGI: registra no stderr metodo, id e traceparent de cada request recebido.

    Fica fora do SDK de proposito, para registrar tambem o que o transporte recusa
    antes de chegar a um handler (por exemplo, `_meta` incompleto).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        mensagens: list[Message] = []
        corpo = b""
        while True:
            mensagem = await receive()
            mensagens.append(mensagem)
            if mensagem["type"] != "http.request":
                break
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body"):
                break
        self._registrar(corpo)
        pendentes = iter(mensagens)

        async def repetir() -> Message:
            return next(pendentes, None) or await receive()

        await self.app(scope, repetir, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            request = json.loads(corpo)
            params = request.get("params") or {}
            meta = params.get("_meta") or {}
            alvo = params.get("name") or params.get("uri")
            linha = f"metodo={request.get('method')} id={request.get('id')!r}"
            if alvo:
                linha += f" alvo={alvo}"
            if "inputResponses" in params:
                linha += " retry=sim"
            capabilities = json.dumps(meta.get("io.modelcontextprotocol/clientCapabilities"), separators=(",", ":"))
            linha += f" traceparent={meta.get('traceparent', '-')} capabilities={capabilities}"
        except (ValueError, AttributeError):
            linha = "corpo que nao e um request JSON-RPC"
        print(f"[mcp] request {linha}", file=sys.stderr, flush=True)


def criar_app() -> ASGIApp:
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=HOST,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"localhost:{PORTA}", f"127.0.0.1:{PORTA}", f"[::1]:{PORTA}"],
            allowed_origins=[f"http://localhost:{PORTA}", f"http://127.0.0.1:{PORTA}"],
        ),
    )
    return RegistroDeRequests(app)


if __name__ == "__main__":
    print(f"[mcp] central-de-salas em http://{HOST}:{PORTA}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(criar_app(), host=HOST, port=PORTA, log_level="warning")
