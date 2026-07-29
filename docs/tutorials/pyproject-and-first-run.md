# Comprendre le projet et lancer sa première observation

Ce guide explique la configuration Python du projet, les outils retenus et l'API actuellement
disponible. Il est conçu pour être lu avec le code ouvert dans un éditeur.

## 1. La place de `pyproject.toml`

`pyproject.toml` est le fichier central d'un projet Python moderne. Il peut contenir trois
catégories d'informations :

1. la manière de construire le package ;
2. les métadonnées et dépendances du package ;
3. la configuration des outils de développement.

TOML est un format de configuration. Ses principales constructions sont :

```toml
[section]                    # une table
name = "appmonitor"          # une chaîne
enabled = true               # un booléen
values = ["a", "b"]          # une liste
author = { name = "doirme" } # une table écrite sur une ligne
```

Les commentaires commencent par `#`. Une section comme `[tool.ruff]` correspond à une table
`tool`, contenant une table `ruff`.

## 2. Lecture ligne par ligne du fichier

### Système de construction

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- `[build-system]` est une section standardisée par PEP 518. Elle indique à `uv`, `pip` ou tout
  autre frontend comment construire le projet.
- `requires = ["hatchling"]` demande la présence de Hatchling dans l'environnement de build
  isolé. Ce n'est pas une dépendance nécessaire à l'utilisateur d'AppMonitor.
- `build-backend = "hatchling.build"` sélectionne l'implémentation PEP 517 appelée pour produire
  une wheel et une archive source.

Il faut distinguer deux rôles :

- **frontend de build** : `uv build` prépare l'environnement et demande une construction ;
- **backend de build** : Hatchling détermine quels fichiers entrent dans le package et produit
  les archives.

### Pourquoi Hatchling ?

Hatchling est le backend du projet Hatch. Il est adapté ici parce qu'il est léger, respecte les
standards Python modernes et gère naturellement une arborescence `src/`. Il ne remplace ni `uv`
ni les tests : son travail se limite principalement à construire et empaqueter.

Alternatives courantes :

| Backend | Positionnement |
| --- | --- |
| Hatchling | Configuration moderne et relativement réduite |
| setuptools | Historique, extrêmement répandu, très flexible |
| Flit | Minimaliste, adapté aux packages simples |
| PDM backend | Backend moderne de l'écosystème PDM |
| maturin | Extensions Python écrites en Rust |

Le choix n'est pas irréversible. Tant que les métadonnées restent standardisées, changer de
backend demande généralement peu de modifications.

### Métadonnées du package

```toml
[project]
name = "appmonitor"
version = "0.1.0"
description = "Deterministic runtime monitoring and maintenance orchestration for Python projects."
readme = "README.md"
requires-python = ">=3.12"
license = "MIT"
authors = [{ name = "doirme" }]
dependencies = ["psutil>=7.0"]
```

- `[project]` est la table de métadonnées standardisée par PEP 621.
- `name` est le nom de distribution. C'est le nom utilisé lors d'une installation et, plus tard,
  pour une éventuelle publication sur PyPI.
- `version` suit ici le versionnage sémantique. `0.1.0` indique une API encore jeune.
- `description` est le résumé court inclus dans les métadonnées du package.
- `readme` désigne la description longue. Hatchling l'intègre aux métadonnées construites.
- `requires-python` empêche l'installation sur une version antérieure à Python 3.12. Le code
  utilise notamment `StrEnum`, disponible nativement à partir de Python 3.11.
- `license` déclare la licence de la distribution. Le texte complet reste dans `LICENSE`.
- `authors` est une liste de tables. Une liste permet de déclarer plusieurs auteurs.
- `dependencies` contient les dépendances nécessaires **à l'exécution**. `psutil` observe les
  processus, leur mémoire, leur CPU, leurs threads et leurs enfants.

La contrainte `psutil>=7.0` fixe un minimum mais pas un maximum. `uv.lock` enregistre ensuite la
version exacte résolue. Le TOML exprime la compatibilité souhaitée ; le lockfile exprime
l'environnement reproductible actuellement retenu.

### Commande installée

```toml
[project.scripts]
appmonitor = "appmonitor.cli:main"
```

- `[project.scripts]` déclare les commandes console installées avec le package.
- la clé `appmonitor` devient la commande saisie dans le terminal ;
- `appmonitor.cli` est le module Python `src/appmonitor/cli.py` ;
- `main` est la fonction appelée dans ce module ;
- `:` sépare le chemin de module du nom de l'objet.

Après `uv sync`, `uv run appmonitor ...` utilise cette entrée sans que nous ayons à écrire un
script shell différent pour Windows, Linux et macOS.

### Dépendances de développement

```toml
[dependency-groups]
dev = [
  "mypy>=1.18",
  "pytest>=8.4",
  "pytest-cov>=7.0",
  "ruff>=0.12",
  "types-psutil>=7.0",
]
```

- `[dependency-groups]` sépare les outils de développement des dépendances runtime.
- `dev` est le groupe installé par `uv sync --dev`.
- `mypy` vérifie statiquement les annotations de types.
- `pytest` découvre et exécute les tests.
- `pytest-cov` connecte pytest à Coverage.py.
- `ruff` regroupe un linter très rapide et un formateur Python.
- `types-psutil` contient les annotations de types de `psutil`. Il est utile à mypy, pas au
  fonctionnement d'AppMonitor.

Un utilisateur qui installe seulement AppMonitor reçoit `psutil`, mais pas pytest, Ruff ou mypy.

### Contenu de la wheel

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/appmonitor"]
```

- toutes les sections commençant par `[tool...]` configurent un outil particulier ;
- cette section est propre à Hatchling ;
- `packages` indique que le package importable est dans `src/appmonitor`.

L'arborescence `src/` évite qu'un test importe accidentellement le dossier courant à la place du
package installé. Les tests sont ainsi plus proches de la situation réelle d'un utilisateur.

### Configuration de pytest

```toml
[tool.pytest.ini_options]
addopts = "--strict-config --strict-markers"
testpaths = ["tests"]
```

- `addopts` ajoute des options à chaque lancement ;
- `--strict-config` transforme les clés de configuration inconnues en erreurs ;
- `--strict-markers` refuse les marqueurs pytest non déclarés, ce qui détecte les fautes de
  frappe ;
- `testpaths` limite la découverte automatique au dossier `tests`.

### Configuration de Coverage.py

```toml
[tool.coverage.run]
branch = true
source = ["appmonitor"]

[tool.coverage.report]
fail_under = 90
show_missing = true
```

- `branch = true` mesure les branches de contrôle, pas uniquement les lignes. Un `if` dont une
  seule branche est testée est donc signalé ;
- `source` limite la mesure au package applicatif ;
- `fail_under = 90` fait échouer la commande sous 90 % de couverture ;
- `show_missing` affiche les lignes non couvertes.

Une couverture élevée ne prouve pas la correction. Elle indique seulement quelle partie du code
a été parcourue. Les assertions et la pertinence des scénarios restent essentielles.

### Configuration de mypy

```toml
[tool.mypy]
python_version = "3.12"
strict = true
packages = ["appmonitor"]
```

- `python_version` analyse le code avec les règles et APIs de Python 3.12 ;
- `strict` active un ensemble exigeant de vérifications : annotations manquantes, `Any` implicite,
  retours incohérents, valeurs optionnelles non vérifiées, etc. ;
- `packages` demande l'analyse récursive du package installé.

La commande du projet ajoute aussi `tests` explicitement :

```powershell
uv run mypy src tests
```

### Configuration générale de Ruff

```toml
[tool.ruff]
line-length = 100
target-version = "py312"
```

- `line-length` fixe la largeur cible à 100 caractères ;
- `target-version` permet à Ruff de proposer des règles compatibles avec Python 3.12.

### Règles du linter

```toml
[tool.ruff.lint]
select = ["ALL"]
ignore = ["COM812", "D203", "D213"]
```

- `select = ["ALL"]` active toutes les familles de règles connues par cette version de Ruff ;
- `COM812` est ignorée pour éviter un conflit de virgules finales avec le formateur ;
- `D203` et `D213` représentent deux styles de docstrings incompatibles avec les styles retenus
  respectivement par `D211` et `D212`.

Activer `ALL` est volontairement strict. Lorsqu'une exception est justifiée, nous préférons une
désactivation locale commentée à l'abandon global d'une famille de contrôles.

### Exception propre aux tests

```toml
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]
"examples/**" = ["T201"]
```

- `per-file-ignores` applique une exception à un ensemble précis de fichiers ;
- `S101` déconseille `assert` dans du code applicatif, car Python peut supprimer les assertions
  avec l'option `-O` ;
- pytest utilise justement `assert` pour produire ses diagnostics. La règle est donc ignorée
  uniquement dans les tests ;
- `T201` interdit normalement `print`, afin que le code de bibliothèque utilise un système de
  logs ou retourne une valeur. Les exemples doivent explicitement produire stdout et stderr pour
  montrer leur capture : cette exception reste donc limitée à `examples/**`.

## 3. Le rôle de `uv`

`uv` est ici le gestionnaire de projet et d'environnement :

```powershell
uv sync --dev       # crée/met à jour .venv depuis pyproject.toml et uv.lock
uv lock             # recalcule uv.lock après une modification des dépendances
uv run pytest       # exécute pytest dans l'environnement du projet
uv build            # demande à Hatchling de construire sdist et wheel
```

Le dossier `.venv` contient l'environnement local et n'est pas commité. `uv.lock` est commité afin
que les développeurs et la CI installent les mêmes versions. `uv sync --frozen` refuse de modifier
le lockfile : c'est le mode à utiliser dans une validation reproductible.

Flux conseillé lors d'un changement de dépendance :

```powershell
# 1. Modifier pyproject.toml
uv lock
uv sync --dev --frozen
uv run pytest --cov --cov-branch
```

## 4. Ordre conseillé pour lire l'API

Le code est volontairement découpé par responsabilité. Lis-le dans cet ordre :

1. `src/appmonitor/models.py` : comprendre l'entrée immutable `RunSpec` ;
2. `src/appmonitor/artifacts.py` : voir les instantanés avant/après ;
3. `src/appmonitor/execution.py` : suivre l'exécution, les threads de lecture et `psutil` ;
4. `src/appmonitor/persistence.py` : voir la transaction SQLite et les tables normalisées ;
5. `src/appmonitor/states.py` : étudier le graphe de transitions déterministe ;
6. `src/appmonitor/cli.py` : voir comment la ligne de commande assemble les objets ;
7. `src/appmonitor/__init__.py` : identifier l'API publique volontairement exposée ;
8. `tests/unit/` : lire le contrat comportemental de chaque module.

Pour chaque fichier, applique la méthode suivante :

1. lire les tests avant l'implémentation ;
2. noter les entrées, sorties et erreurs attendues ;
3. suivre un seul scénario de bout en bout ;
4. placer temporairement un point d'arrêt ou ajouter un test local ;
5. relancer uniquement le test concerné, puis toute la suite.

Exemple de test ciblé :

```powershell
uv run pytest tests/unit/test_execution.py -vv
```

## 5. Hello world : programme observé

Le premier exemple se trouve dans `examples/hello_world/hello.py`. Il écrit sur stdout, stderr et
crée un fichier. Ces trois effets permettent de vérifier les collecteurs.

Depuis la racine du dépôt :

```powershell
uv sync --dev --frozen
uv run appmonitor run --repo examples/hello_world -- `
  python hello.py
```

La commande produit un rapport JSON. Cherche les champs suivants :

- `outcome` doit valoir `succeeded` ;
- `exit_code` doit valoir `0` ;
- `stdout` contient `Hello from AppMonitor` ;
- `stderr` contient le message de diagnostic ;
- `artifacts.created` contient `hello-output.txt` ;
- `metrics` contient les échantillons du processus ;
- `peak_rss_bytes` n'est pas sérialisé directement, car c'est une propriété calculée.

La commande externe `uv run appmonitor` garantit qu'AppMonitor utilise l'environnement du projet.
Le programme cible est volontairement lancé avec `python` pour ne pas inclure le temps, les logs
et les éventuels fichiers de cache d'un second processus `uv` dans les observations. Dans l'API,
`sys.executable` permet de transmettre exactement l'interpréteur courant. Pour l'afficher :

```powershell
uv run python -c "import sys; print(sys.executable)"
```

## 6. Hello world : utilisation Python

Le fichier `examples/hello_world/monitor.py` assemble explicitement l'API :

```powershell
uv run python examples/hello_world/monitor.py
```

Le flux est :

```text
RunSpec -> LocalExecutor.execute -> RunReport -> SQLiteRunStore.save
```

Points importants :

- `RunSpec` valide et normalise les paramètres avant le lancement ;
- `LocalExecutor` retourne un rapport même si le programme cible sort avec un code non nul ;
- une erreur de démarrage, par exemple un exécutable inexistant, reste une exception Python ;
- `SQLiteRunStore.save()` enregistre le rapport complet et les données normalisées dans une même
  transaction ;
- `load()` permet de relire la représentation portable à partir du `run_id`.

La base de l'exemple est créée dans `.appmonitor/hello-world.sqlite3`, dossier ignoré par Git.

## 7. Exercices progressifs

### Exercice A : observer un échec

Crée `examples/hello_world/failure.py` :

```python
raise RuntimeError("intentional failure")
```

Observe `outcome`, `exit_code` et `stderr`. AppMonitor lui-même ne doit pas planter.

### Exercice B : provoquer un timeout

```powershell
uv run appmonitor run --repo examples/hello_world --timeout 0.2 -- `
  python -c "import time; time.sleep(5)"
```

Vérifie `outcome = "timed_out"` et `timed_out = true`.

### Exercice C : distinguer création, modification et suppression

Écris un script qui crée un fichier, modifie `hello-output.txt` et supprime un troisième fichier
préparé avant l'exécution. Inspecte les trois listes de `artifacts`.

### Exercice D : explorer SQLite

Avec Python, sans outil supplémentaire :

```powershell
uv run python -c "import sqlite3; c=sqlite3.connect('.appmonitor/hello-world.sqlite3'); print(c.execute('select run_id, outcome from runs').fetchall())"
```

Puis examine `log_lines`, `metrics` et `artifacts`. Les clés `run_id` relient toutes les tables.

### Exercice E : tester la machine à états

Dans un interpréteur Python :

```python
from appmonitor.states import RunState, RunStateMachine

machine = RunStateMachine()
machine.transition(
    RunState.REPOSITORY_PREPARED,
    cause="repository exists",
    actor="system",
)
print(machine.state)
print(machine.history)
```

Essaie ensuite de passer directement de `CREATED` à `RUNNING`. L'exception démontre que la
décision reste dans l'orchestrateur et non dans un futur agent LLM.

## 8. Modifier le projet selon la méthode test-first

Pour ajouter un comportement :

1. écrire un test qui exprime le contrat ;
2. exécuter le test et vérifier qu'il échoue pour la bonne raison ;
3. écrire l'implémentation minimale ;
4. exécuter le test ciblé ;
5. exécuter tous les contrôles ;
6. mettre à jour la référence API et les exemples ;
7. committer seulement lorsque tout est vert.

Gate complet :

```powershell
uv run pytest --cov --cov-branch
uv run ruff check .
uv run mypy src tests
uv run python -m compileall -q src tests examples
uv build
```

## 9. Ce que cette version ne fait pas encore

Cette version orchestre, observe et persiste une exécution locale. Elle ne fournit pas encore :

- le contrat `goal.yaml` ;
- l'analyse AST, Ruff et mypy d'un dépôt cible ;
- les agents OpenRouter ;
- les worktrees Git ;
- l'exécution Docker.

Cette limite est utile pour l'apprentissage : le socle actuel reste assez petit pour suivre le
chemin complet d'une commande sans abstraction agentique.
