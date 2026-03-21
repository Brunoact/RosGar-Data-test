"""
🧹 db_cleaner.py v1.1 - Corrección de BD (Estrategias A + B)
=============================================================

ESTRATEGIA A: Validación año → modelo (con tolerancia ±2)
  - Holgura de ±2 años antes de cambiar modelo
  - Detección de año basura (>10 años fuera de rango, futuro, etc.)
  - Si año es basura → no cambiar modelo, opcionalmente nullificar año

ESTRATEGIA B: Mapeo de códigos → nombre de catálogo
  - c200 → clase c, 320i → serie 3, gla200 → clase gla

USO:
  python db_cleaner.py                      # Ejecutar corrección
  python db_cleaner.py --dry-run            # Simular sin cambios
  python db_cleaner.py --db otra.db         # BD alternativa
  python db_cleaner.py --verbose            # Más logs
"""

import sqlite3
import shutil
import argparse
import logging
import sys
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import Counter

# ═══════════════════════════════════════════════════════════════
# 📦 IMPORTS
# ═══════════════════════════════════════════════════════════════

try:
    import normalizer_v2 as nv2
    HAS_NV2 = True
except ImportError:
    HAS_NV2 = False

# ═══════════════════════════════════════════════════════════════
# ⚙️ CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

DEFAULT_DB = 'rosariogarage.db'
BACKUP_SUFFIX = '_nofix'
CHANGELOG_TABLE = 'normalization_changelog'

# ── Tolerancia de año ──
YEAR_TOLERANCE = 2          # ±2 años antes de cambiar modelo
YEAR_GARBAGE_THRESHOLD = 10 # >10 años fuera = año basura
CURRENT_YEAR = datetime.now().year
YEAR_MAX_VALID = CURRENT_YEAR + 1
YEAR_MIN_VALID = 1940

# ═══════════════════════════════════════════════════════════════
# 📊 ESTADÍSTICAS
# ═══════════════════════════════════════════════════════════════

@dataclass
class CleanerStats:
    total_vehicles: int = 0
    analyzed: int = 0

    # Estrategia A
    year_issues_found: int = 0
    year_fixes_applied: int = 0
    year_no_suggestion: int = 0
    year_within_tolerance: int = 0
    year_garbage_detected: int = 0
    year_nullified: int = 0

    # Estrategia B
    code_issues_found: int = 0
    code_fixes_applied: int = 0

    # Alias
    marca_alias_fixed: int = 0

    # General
    total_fixes: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"Analizados: {self.analyzed}/{self.total_vehicles}\n"
            f"  Estrategia A (año→modelo):\n"
            f"    - Detectados fuera de rango: {self.year_issues_found}\n"
            f"    - Dentro de tolerancia ±{YEAR_TOLERANCE} (no tocados): "
            f"{self.year_within_tolerance}\n"
            f"    - Año basura (>±{YEAR_GARBAGE_THRESHOLD}): "
            f"{self.year_garbage_detected}\n"
            f"    - Años nullificados: {self.year_nullified}\n"
            f"    - Modelo corregido: {self.year_fixes_applied}\n"
            f"    - Sin sugerencia: {self.year_no_suggestion}\n"
            f"  Estrategia B (código→nombre): "
            f"{self.code_fixes_applied}/{self.code_issues_found}\n"
            f"  Alias de marca: {self.marca_alias_fixed}\n"
            f"  Total fixes: {self.total_fixes} | "
            f"Errores: {self.errors}"
        )


STATS = CleanerStats()

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 🛠️ HELPERS
# ═══════════════════════════════════════════════════════════════

def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s │ %(levelname)-7s │ %(message)s',
        datefmt='%H:%M:%S'
    )


def create_backup(db_path: str) -> str:
    base, ext = os.path.splitext(db_path)
    backup_path = f"{base}{BACKUP_SUFFIX}{ext}"
    shutil.copy2(db_path, backup_path)
    size_mb = os.path.getsize(backup_path) / (1024 * 1024)
    logger.info(f"💾 Backup: {backup_path} ({size_mb:.1f} MB)")
    return backup_path


def setup_changelog(conn):
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {CHANGELOG_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id TEXT NOT NULL,
            campo TEXT NOT NULL,
            valor_anterior TEXT,
            valor_nuevo TEXT,
            estrategia TEXT,
            razon TEXT,
            confidence INTEGER DEFAULT 0,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_cl_vehicle
            ON {CHANGELOG_TABLE}(vehicle_id);
    """)
    conn.commit()


def log_change(conn, vid, campo, old, new, estrategia, razon,
               confidence=0):
    conn.execute(f"""
        INSERT INTO {CHANGELOG_TABLE}
        (vehicle_id, campo, valor_anterior, valor_nuevo,
         estrategia, razon, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (vid, campo,
          str(old) if old else None,
          str(new) if new else None,
          estrategia, razon, confidence))


def normalize_key(text):
    if not text:
        return ""
    return text.lower().strip()


def read_all_vehicles(conn):
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, url, marca, modelo, version, año,
               kilometros, norm_status, activo
        FROM vehicles ORDER BY id
    """).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# 📅 AÑO BASURA (NUEVO v1.1)
# ═══════════════════════════════════════════════════════════════

def is_year_garbage(año, km=None):
    """
    Detecta año basura SIN necesidad de catálogo.
    Para validación rápida antes de buscar en catálogo.
    """
    if not año:
        return False, None

    # Futuro
    if año > YEAR_MAX_VALID:
        return True, f"Año {año} es futuro (máx: {YEAR_MAX_VALID})"

    # Muy antiguo
    if año < YEAR_MIN_VALID:
        return True, f"Año {año} < {YEAR_MIN_VALID}"

    # Km altos + año actual/futuro
    if km and km > 50000 and año >= CURRENT_YEAR:
        return True, (
            f"Año {año} con {km:,} km es imposible"
        )

    return False, None


def is_year_garbage_for_model(año, model_range):
    """
    Detecta año basura respecto al rango del modelo.
    Retorna (is_garbage, distance, reason).
    """
    if not año or not model_range:
        return False, 0, None

    desde = model_range.get('desde', 0)
    hasta = model_range.get('hasta', 9999)

    if desde <= año <= hasta:
        return False, 0, None

    if año < desde:
        distance = desde - año
    else:
        distance = año - hasta

    if distance > YEAR_GARBAGE_THRESHOLD:
        return True, distance, (
            f"Año {año} a {distance} años del rango "
            f"{desde}-{hasta} (umbral: {YEAR_GARBAGE_THRESHOLD})"
        )

    return False, distance, None


# ═══════════════════════════════════════════════════════════════
# 🅰️ ESTRATEGIA A: VALIDACIÓN POR AÑO (con tolerancia)
# ═══════════════════════════════════════════════════════════════

def check_year_range(catalog, marca, modelo, año, km=None):
    """
    Verifica año vs rango del modelo CON TOLERANCIA.

    Returns None si OK, o Dict con resultado:
      - action: 'keep' | 'correct' | 'garbage' | 'no_suggestion'
      - suggestion: modelo sugerido (si action='correct')
      - reason: explicación
    """
    if not año or not marca or not modelo:
        return None

    marca_resolved = catalog.resolve_brand(marca)

    if not catalog.has_brand(marca_resolved):
        return None
    if not catalog.has_model(marca_resolved, modelo):
        return None

    # ── Año basura absoluto (futuro, etc.) ──
    garbage, reason = is_year_garbage(año, km)
    if garbage:
        return {
            'action': 'garbage',
            'suggestion': None,
            'reason': reason,
        }

    # ── Verificar rango del modelo ──
    yr = catalog.get_year_range(marca_resolved, modelo)
    if not yr:
        return None

    desde = yr.get('desde', 0)
    hasta = yr.get('hasta', 9999)

    # Dentro del rango real → OK
    if desde <= año <= hasta:
        return None

    # Dentro de la tolerancia → KEEP (no tocar)
    desde_tol = desde - YEAR_TOLERANCE
    hasta_tol = hasta + YEAR_TOLERANCE

    if desde_tol <= año <= hasta_tol:
        return {
            'action': 'keep',
            'suggestion': None,
            'reason': (
                f"Año {año} fuera de {desde}-{hasta} pero "
                f"dentro de tolerancia ±{YEAR_TOLERANCE}"
            ),
        }

    # ── Verificar si es basura para este modelo ──
    garbage_model, distance, reason = is_year_garbage_for_model(año, yr)
    if garbage_model:
        return {
            'action': 'garbage',
            'suggestion': None,
            'reason': reason,
        }

    # ── Buscar sucesor/predecesor ──
    # Caminar la cadena buscando modelo donde el año
    # caiga DENTRO del rango real (sin tolerancia)

    if año > hasta:
        suggestion = _walk_successors(
            catalog, marca_resolved, modelo, año
        )
    else:
        suggestion = _walk_predecessors(
            catalog, marca_resolved, modelo, año
        )

    if suggestion:
        sug_yr = catalog.get_year_range(marca_resolved, suggestion)
        sd = sug_yr.get('desde', 0) if sug_yr else '?'
        sh = sug_yr.get('hasta', 9999) if sug_yr else '?'
        return {
            'action': 'correct',
            'suggestion': suggestion,
            'reason': (
                f"'{modelo}' existió {desde}-{hasta}, "
                f"año {año} → '{suggestion}' ({sd}-{sh})"
            ),
        }

    return {
        'action': 'no_suggestion',
        'suggestion': None,
        'reason': (
            f"Año {año} fuera de {desde}-{hasta} para '{modelo}', "
            f"sin sucesor/predecesor que cubra ese año"
        ),
    }


def _walk_successors(catalog, marca, modelo, año, max_depth=5):
    current = modelo
    visited = {current}
    for _ in range(max_depth):
        sucesor = catalog.get_successor(marca, current)
        if not sucesor or sucesor in visited:
            break
        visited.add(sucesor)
        if catalog.has_model(marca, sucesor):
            syr = catalog.get_year_range(marca, sucesor)
            if syr:
                sd = syr.get('desde', 0)
                sh = syr.get('hasta', 9999)
                if sd <= año <= sh:
                    return sucesor
        current = sucesor
    return None


def _walk_predecessors(catalog, marca, modelo, año, max_depth=5):
    current = modelo
    visited = {current}
    for _ in range(max_depth):
        pred = catalog.get_predecessor(marca, current)
        if not pred or pred in visited:
            break
        visited.add(pred)
        if catalog.has_model(marca, pred):
            pyr = catalog.get_year_range(marca, pred)
            if pyr:
                pd = pyr.get('desde', 0)
                ph = pyr.get('hasta', 9999)
                if pd <= año <= ph:
                    return pred
        current = pred
    return None


# ═══════════════════════════════════════════════════════════════
# 🅱️ ESTRATEGIA B: MAPEO DE CÓDIGOS
# ═══════════════════════════════════════════════════════════════

def check_code_mapping(catalog, marca, modelo):
    """
    Si el modelo es un código → mapear a nombre de catálogo.
    Ejemplo: "c200" → "clase c"
    """
    if not marca or not modelo:
        return None

    marca_resolved = catalog.resolve_brand(marca)
    modelo_norm = normalize_key(modelo)

    if catalog.has_model(marca_resolved, modelo_norm):
        return None

    code_mappings = catalog.code_mappings.get(
        normalize_key(marca_resolved), {}
    )
    if not code_mappings:
        return None

    # Match exacto del modelo como código
    if modelo_norm in code_mappings:
        mapped = code_mappings[modelo_norm]
        if isinstance(mapped, dict):
            if mapped.get('ambiguo'):
                return None
            mapped_model = mapped.get('modelos', [None])[0]
        else:
            mapped_model = mapped

        if mapped_model and catalog.has_model(
            marca_resolved, mapped_model
        ):
            return {
                'mapped_model': mapped_model,
                'reason': (
                    f"Código '{modelo}' → '{mapped_model}'"
                ),
            }

    # Extraer prefijo
    prefixes = []

    # letras+números: c200, gla250
    m = re.match(r'^([a-z]+)[\s\-]?(\d+)', modelo_norm)
    if m:
        prefixes.append(m.group(1))
        prefixes.append(f"{m.group(1)}{m.group(2)}")

    # número+letra: 320i
    m = re.match(r'^(\d)(\d{2})[a-z]?$', modelo_norm)
    if m:
        prefixes.append(m.group(1))

    # x1, m3, z4, rs3, etc
    m = re.match(r'^(x\d|m\d|z\d|i\d|rs\d|ix\d?)', modelo_norm)
    if m:
        prefixes.append(m.group(1))

    # a1, q5, s3, tt
    m = re.match(r'^([aqst]{1,2}\d?)', modelo_norm)
    if m:
        prefixes.append(m.group(1))

    for prefix in prefixes:
        if prefix in code_mappings:
            mapped = code_mappings[prefix]
            if isinstance(mapped, dict):
                if mapped.get('ambiguo'):
                    continue
                mapped_model = mapped.get('modelos', [None])[0]
            else:
                mapped_model = mapped

            if mapped_model and catalog.has_model(
                marca_resolved, mapped_model
            ):
                return {
                    'mapped_model': mapped_model,
                    'reason': (
                        f"Código '{modelo}' (prefijo '{prefix}') "
                        f"→ '{mapped_model}'"
                    ),
                }

    return None


# ═══════════════════════════════════════════════════════════════
# 🔄 PROCESO PRINCIPAL
# ═══════════════════════════════════════════════════════════════

def process_vehicle(v, catalog, conn, dry_run=False):
    vid = v['id']
    marca = normalize_key(v.get('marca') or '')
    modelo = normalize_key(v.get('modelo') or '')
    version = v.get('version') or ''
    año = v.get('año')
    km = v.get('kilometros')
    changes = []

    if not marca:
        return changes

    # ── PASO 0: Resolver alias de marca ──
    marca_resolved = catalog.resolve_brand(marca)
    if marca_resolved != marca and catalog.has_brand(marca_resolved):
        changes.append({
            'campo': 'marca', 'old': marca, 'new': marca_resolved,
            'estrategia': 'alias_marca',
            'reason': f"Alias: '{marca}' → '{marca_resolved}'",
            'confidence': 95,
        })
        marca = marca_resolved
        STATS.marca_alias_fixed += 1

    if not modelo:
        return changes

    # ── PASO 1: Detectar año basura ──
    año_is_garbage = False
    if año:
        garbage, reason = is_year_garbage(año, km)
        if garbage:
            año_is_garbage = True
            STATS.year_garbage_detected += 1
            changes.append({
                'campo': 'año', 'old': año, 'new': None,
                'estrategia': 'year_garbage',
                'reason': f"Año basura: {reason}",
                'confidence': 90,
            })
            STATS.year_nullified += 1
            logger.debug(f"  🗑️ [{vid}] {reason}")

    # ── PASO 2: Estrategia B (código→nombre) ──
    code_result = check_code_mapping(catalog, marca, modelo)
    if code_result:
        STATS.code_issues_found += 1
        mapped = code_result['mapped_model']
        changes.append({
            'campo': 'modelo', 'old': modelo, 'new': mapped,
            'estrategia': 'B_code_mapping',
            'reason': code_result['reason'],
            'confidence': 90,
        })
        modelo = mapped
        STATS.code_fixes_applied += 1

    # ── PASO 3: Estrategia A (año→modelo, con tolerancia) ──
    if año and not año_is_garbage:
        year_result = check_year_range(
            catalog, marca, modelo, año, km
        )
        if year_result:
            action = year_result['action']

            if action == 'keep':
                STATS.year_within_tolerance += 1
                logger.debug(
                    f"  ✅ [{vid}] {year_result['reason']}"
                )

            elif action == 'correct':
                STATS.year_issues_found += 1
                suggestion = year_result['suggestion']
                changes.append({
                    'campo': 'modelo',
                    'old': modelo,
                    'new': suggestion,
                    'estrategia': 'A_year_validation',
                    'reason': year_result['reason'],
                    'confidence': 85,
                })
                STATS.year_fixes_applied += 1

            elif action == 'garbage':
                STATS.year_garbage_detected += 1
                changes.append({
                    'campo': 'año', 'old': año, 'new': None,
                    'estrategia': 'year_garbage_model',
                    'reason': year_result['reason'],
                    'confidence': 85,
                })
                STATS.year_nullified += 1

            elif action == 'no_suggestion':
                STATS.year_issues_found += 1
                STATS.year_no_suggestion += 1
                logger.debug(
                    f"  ⚠️ [{vid}] {year_result['reason']}"
                )

    # ── Aplicar cambios ──
    if changes and not dry_run:
        final_values = {}
        for change in changes:
            campo = change['campo']
            final_values[campo] = change['new']
            log_change(
                conn, vid, campo,
                change['old'], change['new'],
                change['estrategia'], change['reason'],
                change['confidence']
            )

        # Recalcular norm_status
        new_marca = final_values.get('marca', marca)
        new_modelo = final_values.get('modelo', modelo)

        if new_modelo and catalog.has_model(new_marca, new_modelo):
            versions = catalog.get_versions(new_marca, new_modelo)
            version_norm = normalize_key(version)
            has_ver = version_norm in [
                normalize_key(v) for v in versions
            ] if versions else False
            final_values['norm_status'] = (
                'full_match' if has_ver else 'partial_match'
            )
        else:
            final_values['norm_status'] = 'fallback'

        sets = ', '.join(f"{k} = ?" for k in final_values)
        vals = list(final_values.values()) + [vid]
        conn.execute(
            f"UPDATE vehicles SET {sets} WHERE id = ?", vals
        )
        STATS.total_fixes += len(changes)

    return changes


# ═══════════════════════════════════════════════════════════════
# 🚀 RUN
# ═══════════════════════════════════════════════════════════════

def run_cleaner(db_path, dry_run=False,
                dicts_path='config/normalizer_dicts.json'):
    global STATS
    STATS = CleanerStats()

    logger.info("🧹 " + "=" * 58)
    logger.info("🧹 DB CLEANER v1.1 — Estrategias A + B")
    logger.info("🧹 " + "=" * 58)
    logger.info(
        f"   Tolerancia de año: ±{YEAR_TOLERANCE} | "
        f"Umbral basura: ±{YEAR_GARBAGE_THRESHOLD}"
    )

    if dry_run:
        logger.info("   ⚡ MODO DRY-RUN")

    if not os.path.exists(db_path):
        logger.error(f"❌ BD no encontrada: {db_path}")
        sys.exit(1)

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    logger.info(f"📂 BD: {db_path} ({size_mb:.1f} MB)")

    if not HAS_NV2:
        logger.error("❌ normalizer_v2.py no encontrado")
        sys.exit(1)

    loaded = nv2.init_normalizer(dicts_path=dicts_path)
    if not loaded:
        logger.error(f"❌ Catálogo no cargado: {dicts_path}")
        sys.exit(1)
    catalog = nv2.get_catalog()
    logger.info("✅ Catálogo cargado")

    if not dry_run:
        create_backup(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    setup_changelog(conn)

    vehicles = read_all_vehicles(conn)
    STATS.total_vehicles = len(vehicles)
    logger.info(f"📊 Vehículos: {len(vehicles)}")

    examples_a = []
    examples_b = []
    examples_garbage = []
    examples_tolerance = []

    batch_size = 500
    for i in range(0, len(vehicles), batch_size):
        batch = vehicles[i:i + batch_size]
        for v in batch:
            STATS.analyzed += 1
            try:
                changes = process_vehicle(v, catalog, conn, dry_run)
                for c in changes:
                    ex = {'id': v['id'], 'año': v.get('año'), **c}
                    if (c['estrategia'] == 'A_year_validation'
                            and len(examples_a) < 10):
                        examples_a.append(ex)
                    elif (c['estrategia'] == 'B_code_mapping'
                            and len(examples_b) < 10):
                        examples_b.append(ex)
                    elif ('garbage' in c['estrategia']
                            and len(examples_garbage) < 10):
                        examples_garbage.append(ex)
            except Exception as e:
                STATS.errors += 1
                logger.debug(f"Error {v['id']}: {e}")

        if not dry_run:
            conn.commit()

        processed = min(i + batch_size, len(vehicles))
        logger.info(
            f"   {processed}/{len(vehicles)} | "
            f"Fixes: {STATS.total_fixes}"
        )

    if not dry_run:
        conn.commit()

    # ── Reporte ──
    logger.info(f"\n{'═' * 60}")
    logger.info("📊 REPORTE DE LIMPIEZA")
    logger.info(f"{'═' * 60}")
    logger.info(f"\n{STATS.summary()}")

    if examples_garbage:
        logger.info(f"\n🗑️ Ejemplos - Año basura:")
        for ex in examples_garbage[:5]:
            logger.info(
                f"   [{ex['id']}] año {ex.get('año')}: "
                f"{ex['reason']}"
            )

    if examples_b:
        logger.info(f"\n🅱️ Ejemplos - Código → catálogo:")
        for ex in examples_b[:5]:
            logger.info(
                f"   [{ex['id']}] {ex['old']} → {ex['new']}"
            )

    if examples_a:
        logger.info(f"\n🅰️ Ejemplos - Corrección por año:")
        for ex in examples_a[:5]:
            logger.info(
                f"   [{ex['id']}] año {ex.get('año')}: "
                f"{ex['old']} → {ex['new']}"
            )
            logger.info(f"      ↳ {ex['reason']}")

    # Estado final
    if not dry_run:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            "SELECT COUNT(*) as c FROM vehicles"
        ).fetchone()['c']
        logger.info(f"\n📋 BD final: {total} vehículos")

        rows = conn.execute("""
            SELECT norm_status, COUNT(*) as c
            FROM vehicles GROUP BY norm_status
            ORDER BY c DESC
        """).fetchall()
        for row in rows:
            logger.info(
                f"   {row['norm_status'] or 'NULL'}: {row['c']}"
            )

        cl = conn.execute(
            f"SELECT COUNT(*) as c FROM {CHANGELOG_TABLE}"
        ).fetchone()['c']
        logger.info(f"\n📝 Changelog: {cl} entradas")

    conn.close()

    if dry_run:
        logger.info(
            f"\n⚡ DRY-RUN: {STATS.total_fixes} cambios "
            f"identificados, ninguno aplicado"
        )

    logger.info(f"\n{'═' * 60}")
    logger.info("✅ COMPLETADO")
    logger.info(f"{'═' * 60}")
    return STATS


def parse_args():
    parser = argparse.ArgumentParser(
        description='🧹 Limpieza de BD v1.1 — Estrategias A + B'
    )
    parser.add_argument(
        '--db', default=DEFAULT_DB,
        help=f'Ruta a la BD (default: {DEFAULT_DB})'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Simular sin aplicar'
    )
    parser.add_argument(
        '--dicts-path', default='config/normalizer_dicts.json',
        help='Ruta al JSON de diccionarios'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Más logs'
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)
    stats = run_cleaner(
        db_path=args.db,
        dry_run=args.dry_run,
        dicts_path=args.dicts_path,
    )
    if stats.errors > stats.total_vehicles * 0.1:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
