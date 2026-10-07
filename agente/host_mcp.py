"""O agente como host MCP: um cliente do SDK falando Streamable HTTP com o servidor de salas.

Nada aqui conhece sala, politica ou conflito. O host descobre as tools, le o
resource e repassa o `tools/call`, devolvendo o `input_required` cru para quem
chamou: quem decide o que fazer com a pergunta e a ponte, nao o cliente MCP.
"""

from __future__ import annotations

import contextlib
import itertools
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

PROTOCOLO = "2026-07-28"
RECURSO_DA_POLITICA = "politica://uso"


class SessaoComElicitationEmFormulario(ClientSession):
    """Declara exatamente `{"elicitation": {"form": {}}}`.

    O SDK so anuncia elicitation quando recebe um `elicitation_callback`, e ai
    anuncia form e url juntos. O agente nao quer nenhuma das duas coisas: um
    callback responderia a pergunta sozinho, sem pausar a Task, e ele nao atende
    elicitation em url mode.
    """

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        capabilities = super()._build_capabilities(version)
        capabilities.elicitation = types.ElicitationCapability(form=types.FormElicitationCapability())
        return capabilities


def novo_traceparent(trace_id: str, flags: str = "01") -> str:
    """Mesmo trace-id do cliente A2A, span-id novo a cada request MCP."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"


class DespachanteComIdsDoProcesso(JSONRPCDispatcher):
    """Ids JSON-RPC unicos no processo do agente, e nao so dentro de uma sessao.

    O contador do SDK recomeca a cada sessao. Como o agente abre uma sessao por
    request A2A, o retry de um `tools/call` poderia repetir o id do request
    inicial. Com um contador so, o id do retry e sempre novo.
    """

    _ids = itertools.count(1)

    def _allocate_id(self) -> int:
        return next(self._ids)


class HostMCP:
    """Cria os clientes MCP do agente. O que fica vivo entre chamadas e o cliente HTTP
    (o pool de conexoes); estado de protocolo nao ha o que guardar."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._http = httpx2.AsyncClient(timeout=httpx2.Timeout(30.0))

    async def fechar(self) -> None:
        await self._http.aclose()

    @contextlib.asynccontextmanager
    async def sessao(self) -> AsyncIterator[ClienteMCP]:
        """Uma sessao cliente do SDK, aberta e fechada dentro de um request A2A.

        Curta de proposito: o transporte Streamable HTTP do SDK nao sobrevive a um
        erro de conexao, e uma sessao unica deixaria o agente mudo depois de um
        restart do servidor MCP. Sem handshake: a revisao 2026-07-28 e adotada
        direto, e cada request leva no `_meta` tudo que o servidor precisa.
        """
        async with streamable_http_client(self.url, http_client=self._http) as (leitura, escrita):
            sessao = SessaoComElicitationEmFormulario(
                dispatcher=DespachanteComIdsDoProcesso(leitura, escrita),
                client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
            )
            async with sessao:
                sessao.adopt(
                    types.DiscoverResult(
                        supported_versions=[PROTOCOLO],
                        capabilities=types.ServerCapabilities(),
                        result_type="complete",
                        ttl_ms=0,
                        cache_scope="public",
                    )
                )
                yield ClienteMCP(sessao)


class ClienteMCP:
    """As tres coisas que o agente pede ao servidor MCP. Todo request leva o `traceparent`."""

    def __init__(self, sessao: ClientSession) -> None:
        self.sessao = sessao

    async def descobrir_tools(self, traceparent: str) -> dict[str, types.Tool]:
        resultado = await self.sessao.list_tools(
            params=types.PaginatedRequestParams(_meta={"traceparent": traceparent})
        )
        return {tool.name: tool for tool in resultado.tools}

    async def ler_versao_da_politica(self, traceparent: str) -> str:
        resultado = await self.sessao.read_resource(RECURSO_DA_POLITICA, meta={"traceparent": traceparent})
        texto = next((c.text for c in resultado.contents if isinstance(c, types.TextResourceContents)), "")
        primeira_linha = texto.splitlines()[0] if texto else ""
        rotulo, _, versao = primeira_linha.partition(":")
        if rotulo.strip() != "versao" or not versao.strip():
            raise ValueError(f"o resource {RECURSO_DA_POLITICA} nao declara a versao na primeira linha")
        return versao.strip()

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        traceparent: str,
        *,
        respostas: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """`allow_input_required=True` e o que mantem o MRTR nas maos do agente:
        o `input_required` volta sem ser respondido por callback nenhum."""
        resultado = await self.sessao.call_tool(
            nome,
            argumentos,
            input_responses=respostas,
            request_state=request_state,
            meta={"traceparent": traceparent},
            allow_input_required=True,
        )
        assert isinstance(resultado, types.CallToolResult | types.InputRequiredResult)
        return resultado
