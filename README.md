# Hubuzz

Aplicacao unificada (Opcao B): um unico servico Flask serve o **Hub de
Oportunidades** e o **Agente DOU**, com o Hub consumindo dados reais do
Diario Oficial da Uniao via API interna.

## Rotas
| Rota | Descricao |
|------|-----------|
| `/` | Hub de Oportunidades |
| `/agente` | Painel do Agente DOU |
| `/api/health` | Healthcheck |
| `/api/dou/buscar` (POST) | Busca real no DOU |

## Rodar localmente
```bash
pip install -r requirements.txt
python app.py
# http://localhost:5000
```

## Deploy no Render
Conecte o repositorio. O `render.yaml` ja define build e start.
URL unica: `https://hubuzz.onrender.com`
