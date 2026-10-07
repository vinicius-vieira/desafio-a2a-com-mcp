"""Dominio da Central de Salas: salas, politica de uso e reservas em memoria.

Toda regra de negocio mora aqui, do lado do servidor MCP. O agente nao conhece
nenhuma delas.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

DADOS = Path(os.environ.get("DADOS_DIR") or Path(__file__).resolve().parent.parent / "dados")

FUSO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_DE_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeRegra(ToolError):
    """Violacao de regra de negocio: vira erro de execucao da tool (`isError: true`)."""


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime
    fim: datetime

    def sobrepoe(self, outro: Intervalo) -> bool:
        return self.inicio < outro.fim and outro.inicio < self.fim


def _instante(valor: str, campo: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        raise ErroDeRegra(f"Data invalida em {campo}: use ISO 8601, como 2026-11-03T14:00:00-03:00") from None
    # Sem offset, vale o fuso da politica.
    return instante if instante.tzinfo else instante.replace(tzinfo=FUSO)


def _na_janela(instante: datetime) -> bool:
    return ABERTURA <= instante.astimezone(FUSO).time() <= FECHAMENTO


class CentralDeSalas:
    def __init__(self, dados: Path = DADOS) -> None:
        self.salas: list[dict] = json.loads((dados / "salas.json").read_text(encoding="utf-8"))
        self.reservas: list[dict] = json.loads((dados / "reservas.json").read_text(encoding="utf-8"))
        self.politica = (dados / "politica-de-uso.md").read_text(encoding="utf-8")
        self.versao_da_politica = self.politica.splitlines()[0].partition(":")[2].strip()
        self._por_id = {sala["id"]: sala for sala in self.salas}
        self._sequencia = max((int(r["id"].rpartition("-")[2]) for r in self.reservas), default=0)

    def validar(self, sala: str, inicio: str, fim: str) -> Intervalo:
        """Aplica as regras de sala e de politica, na ordem em que o enunciado as lista."""
        if sala not in self._por_id:
            raise ErroDeRegra(f"Sala inexistente: {sala}")
        intervalo = Intervalo(_instante(inicio, "inicio"), _instante(fim, "fim"))
        if intervalo.fim <= intervalo.inicio:
            raise ErroDeRegra(ERRO_INTERVALO)
        if not (_na_janela(intervalo.inicio) and _na_janela(intervalo.fim)):
            raise ErroDeRegra(ERRO_JANELA)
        if intervalo.fim - intervalo.inicio > DURACAO_MAXIMA:
            raise ErroDeRegra(ERRO_DURACAO)
        return intervalo

    def conflitos(self, sala: str, intervalo: Intervalo) -> list[dict]:
        return [
            r
            for r in self.reservas
            if r["sala"] == sala
            and intervalo.sobrepoe(Intervalo(_instante(r["inicio"], "inicio"), _instante(r["fim"], "fim")))
        ]

    def alternativas(self, sala: str, intervalo: Intervalo) -> list[str]:
        """Salas livres no intervalo, com capacidade >= a da pedida: no maximo tres,
        por capacidade crescente e, em empate, por id."""
        minimo = self._por_id[sala]["capacidade"]
        candidatas = [
            s
            for s in self.salas
            if s["id"] != sala and s["capacidade"] >= minimo and not self.conflitos(s["id"], intervalo)
        ]
        candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
        return [s["id"] for s in candidatas[:MAXIMO_DE_ALTERNATIVAS]]

    def reservar(self, sala: str, inicio: str, fim: str, responsavel: str) -> dict:
        intervalo = self.validar(sala, inicio, fim)
        if self.conflitos(sala, intervalo):
            raise ErroDeRegra(f"Sala ocupada no intervalo: {sala}")
        self._sequencia += 1
        reserva = {
            "id": f"res-{self._sequencia:04d}",
            "sala": sala,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
        }
        self.reservas.append(reserva)
        return reserva
