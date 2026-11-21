# Publicar o site em transcricoes.brasil.io (GitHub Pages)

O site é a pasta `docs/`, gerada pelo `gera_site.py`. O GitHub Pages serve essa pasta direto do branch `main`.

## 1. Repositório

- [ ] Criar `PythonicCafe/transcricoes.brasil.io` no GitHub, **público** e vazio (sem README nem licença)
- [ ] Gerar o site e conferir que `docs/dados/` entrou no commit (são 2 arquivos JSON):

```sh
python gera_site.py --dados ~/projects/transcricoes/dados \
    --csv ~/projects/transcricoes/dados/videos-sessoes-legislativas.csv \
    --excluir _M8DeaEaUuo,nBlqVDlwHWU
git add docs && git status --short docs   # precisa listar docs/dados/indice.json e docs/dados/transcricoes.json
git commit -m "Gera o site"
git remote add origin git@github.com:PythonicCafe/transcricoes.brasil.io.git
git push -u origin main                   # ou: git push -f origin main, se o histórico foi refeito
```

## 2. GitHub Pages

- [ ] No repositório: Settings > Pages > Build and deployment > Source: **Deploy from a branch**
- [ ] Branch: `main`, pasta: `/docs` > Save
- [ ] Em Custom domain deve aparecer `transcricoes.brasil.io` (vem do arquivo `docs/CNAME`). Se não aparecer, digitar e salvar

## 3. DNS do brasil.io

- [ ] Criar o registro: `transcricoes` `CNAME` `pythoniccafe.github.io.`
- [ ] Se o DNS estiver na Cloudflare: deixar o proxy **desligado** (nuvem cinza, "DNS only") até o certificado sair
- [ ] Conferir: `dig +short transcricoes.brasil.io CNAME` deve responder `pythoniccafe.github.io.`

## 4. HTTPS

- [ ] Voltar em Settings > Pages e esperar o aviso "DNS check successful"
- [ ] Marcar **Enforce HTTPS** quando a opção ficar disponível. O certificado é emitido pelo GitHub depois que o DNS
      propaga; costuma levar minutos, mas pode demorar mais
- [ ] Testar: `curl -sI https://transcricoes.brasil.io/ | head -3` e abrir o site no navegador

## 5. Se o domínio ou o certificado não ficarem prontos a tempo

Gerar sem o arquivo `CNAME` e usar o endereço padrão do GitHub. Os caminhos do site são relativos, então funciona
igual:

```sh
python gera_site.py --dominio "" ...      # mesmos argumentos de antes
git rm --cached -q docs/CNAME; rm -f docs/CNAME
git add docs && git commit -m "Publica sem domínio próprio" && git push
```

- [ ] Em Settings > Pages, apagar o Custom domain
- [ ] Endereço: <https://pythoniccafe.github.io/transcricoes.brasil.io/>

## 6. Atualizar o site

Sempre que o pipeline terminar mais vídeos: rodar o `gera_site.py` de novo, `git add docs`, commit e push. O Pages
publica em cerca de um minuto.

## Player do YouTube (erro 153)

O YouTube recusa o player embutido quando a página não envia o cabeçalho `Referer`. O `<iframe>` do site já define
`referrerpolicy="strict-origin-when-cross-origin"`, que resolve isso em páginas servidas por HTTP ou HTTPS. Não há
nada a configurar no domínio. Abrir o `index.html` direto do disco (`file://`) continua dando erro: para testar
localmente, use `python -m http.server` dentro de `docs/`.

## Opcional, depois da submissão

- [ ] Verificar o domínio na organização (Settings da organização > Pages > Add a domain), para ninguém mais poder
      apontar um repositório para `transcricoes.brasil.io`
