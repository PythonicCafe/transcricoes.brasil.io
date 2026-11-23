"""Compara duas medições do `mede_site.py` (antes e depois) e imprime uma tabela em Markdown por perfil."""

import argparse
import json
from pathlib import Path


def kb(quantidade: float) -> str:
    return f"{quantidade / 2**20:.2f} MB" if quantidade >= 2**20 else f"{quantidade / 2**10:.0f} KB"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("antes", type=Path)
    parser.add_argument("depois", type=Path)
    args = parser.parse_args()
    antes = json.loads(args.antes.read_text(encoding="utf-8"))
    depois = json.loads(args.depois.read_text(encoding="utf-8"))
    diferencas = []
    for perfil, cenarios in antes["perfis"].items():
        print(f"\n## {perfil}\n")
        print(
            "| Cenário | Passo | Tempo antes | Tempo depois | Rede antes | Rede depois | Memória JS antes | Memória JS depois |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for cenario, medida in cenarios.items():
            if cenario not in depois["perfis"][perfil]:
                continue
            for passo_antes, passo_depois in zip(
                medida["passos"], depois["perfis"][perfil][cenario]["passos"], strict=True
            ):
                print(
                    f"| {cenario} | {passo_antes['passo']} | {passo_antes['segundos']:.2f} s | {passo_depois['segundos']:.2f} s"
                    f" | {kb(passo_antes['transferido'])} | {kb(passo_depois['transferido'])}"
                    f" | {kb(passo_antes['memoria_js'])} | {kb(passo_depois['memoria_js'])} |"
                )
                if passo_antes["estado"] != passo_depois["estado"]:
                    diferencas.append(
                        (perfil, cenario, passo_antes["passo"], passo_antes["estado"], passo_depois["estado"])
                    )
            total_antes = medida["passos"][-1]["transferido_total"]
            total_depois = depois["perfis"][perfil][cenario]["passos"][-1]["transferido_total"]
            tempo_antes = sum(passo["segundos"] for passo in medida["passos"])
            tempo_depois = sum(passo["segundos"] for passo in depois["perfis"][perfil][cenario]["passos"])
            print(
                f"| {cenario} | **total** | **{tempo_antes:.2f} s** | **{tempo_depois:.2f} s** | **{kb(total_antes)}** | **{kb(total_depois)}** | | |"
            )
    for perfil, cenarios in depois["perfis"].items():
        for cenario, medida in cenarios.items():
            if cenario not in antes["perfis"][perfil]:
                for passo in medida["passos"]:
                    print(
                        f"só no depois [{perfil}] {cenario} | {passo['passo']} | {passo['segundos']:.2f} s | {kb(passo['transferido'])} | {kb(passo['memoria_js'])}"
                    )
    print(f"\nPassos em que a tela mostrou algo diferente: {len(diferencas)}")
    for diferenca in diferencas:
        print(" ", diferenca)


if __name__ == "__main__":
    main()
