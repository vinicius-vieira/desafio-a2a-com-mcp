"""A Task do A2A: identidade, estado, historico e produto, guardados em memoria.

O que e publico (devolvido ao cliente A2A) sai de `Tarefa.publica()`. O resto e
do agente, incluindo a `Pausa` com o `requestState` do MCP, que nunca e serializada.
"""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"

TERMINAIS = {COMPLETED, CANCELED, FAILED}
TRANSICOES = {
    SUBMITTED: {WORKING},
    WORKING: {INPUT_REQUIRED, COMPLETED, CANCELED, FAILED},
    INPUT_REQUIRED: {WORKING, INPUT_REQUIRED},
}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pausa:
    """O que o agente guarda de um `input_required` do MCP enquanto a Task espera.

    `request_state` e opaco: fica aqui, volta no retry byte a byte, e nao e
    lido nem devolvido ao cliente A2A.
    """

    chave: str
    campo: str
    alternativas: list[str]
    request_state: str | None


@dataclass
class Tarefa:
    contexto: str
    trace_id: str
    trace_flags: str = "01"
    id: str = field(default_factory=lambda: novo_id("task"))
    estado: str = SUBMITTED
    mensagem: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artefatos: list[dict[str, Any]] = field(default_factory=list)
    ferramenta: str = ""
    argumentos: dict[str, Any] = field(default_factory=dict)
    politica: str = ""
    pausa: Pausa | None = None

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def mover(self, estado: str, texto: str | None = None) -> None:
        """Transicao de estado. Estado terminal e definitivo: nao ha aresta saindo dele."""
        if estado not in TRANSICOES.get(self.estado, set()):
            raise RuntimeError(f"transicao invalida da Task {self.id}: {self.estado} -> {estado}")
        print(f"[agente] {self.id} {self.estado} -> {estado}", file=sys.stderr, flush=True)
        self.estado = estado
        self.mensagem = None
        if texto is not None:
            self.mensagem = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.contexto,
            }
            self.historico.append(self.mensagem)

    def publica(self, tamanho_do_historico: int | None = None) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem is not None:
            status["message"] = self.mensagem
        historico = self.historico
        if tamanho_do_historico is not None:
            historico = historico[-tamanho_do_historico:] if tamanho_do_historico > 0 else []
        return {
            "id": self.id,
            "contextId": self.contexto,
            "status": status,
            "history": historico,
            "artifacts": self.artefatos,
        }


class Tarefas:
    def __init__(self) -> None:
        self._por_id: dict[str, Tarefa] = {}

    def abrir(self, contexto: str | None, trace_id: str, trace_flags: str) -> Tarefa:
        tarefa = Tarefa(contexto=contexto or novo_id("ctx"), trace_id=trace_id, trace_flags=trace_flags)
        self._por_id[tarefa.id] = tarefa
        print(f"[agente] {tarefa.id} aberta em {SUBMITTED} trace-id={trace_id}", file=sys.stderr, flush=True)
        return tarefa

    def buscar(self, id: str) -> Tarefa | None:
        return self._por_id.get(id)
