"""
🧹 db_cleaner.py v2.0 - Corrección de BD (Estrategias A + B + C)
=================================================================

ESTRATEGIA A: Validación año → modelo (con tolerancia ±2)
  - Holgura de ±2 años antes de cambiar modelo
  - Detección de año basura (>10 años fuera de rango, futuro, etc.)
  - Si año es basura → no cambiar modelo, opcionalmente nullificar año

ESTRATEGIA B: Mapeo de códigos → nombre de catálogo
  - c200 → clase c, 320i → serie 3, gla200 → clase gla

ESTRATEGIA C: Re-matching de versiones (NUEVO v2.0)
  - Seleccionar registros norm_status = 'partial_match'
  - Tomar version_raw (nuevo campo) o reconstruir desde datos existentes
  - Pasar por el normalizer mejorado con matching por componentes
  - Si matchea → actualizar version, subir norm_status a full_match
  - Verificación de coherencia versión↔modelo

USO:
  python db_cleaner.py                      # Ejecutar corrección
  python db_cleaner.py --dry-run            # Simular sin cambios
  python db_cleaner.py --db otra.db         # BD alternativa
  python db_cleaner.py --verbose            # Más logs
  python db_cleaner.py --skip-c             # Saltar Estrategia C
  python db_cleaner.py --only-c             # Solo Estrategia C
"""

import sqlite3
import shutil
import argparse
import logging
import sys
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
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
YEAR_TOLERANCE = 2
YEAR_GARBAGE_THRESHOLD = 10
CURRENT_YEAR = datetime.now().year
YEAR_MAX_VALID = CURRENT_YEAR + 1
YEAR_MIN_VALID = 1940

# ── Estrategia C ──
STRATEGY_C_BATCH_SIZE = 500
STRATEGY_C_MIN_CONFIDENCE = 50


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

    # Estrategia C (NUEVO)
    strategy_c_candidates: int = 0
    strategy_c_rematched: int = 0
    strategy_c_upgraded: int = 0
    strategy_c_confidence_lowered: int = 0
    strategy_c_skipped: int = 0

    # Alias
    marca_alias_fixed: int = 0

    # Extracción de metadata (NUEVO)
    metadata_extracted: int = 0
    gnc_detected: int = 0
    es_0km_detected: int = 0
    puertas_extracted: int = 0
    traccion_extracted: int = 0

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
            f"  Estrategia C (re-match versiones):\n"
            f"    - Candidatos: {self.strategy_c_candidates}\n"
            f"    - Re-matcheados: {self.strategy_c_rematched}\n"
            f"    - Subidos a full_match: {self.strategy_c_upgraded}\n"
            f"    - Confidence bajado: {self.strategy_c_confidence_lowered}\n"
            f"    - Saltados: {self.strategy_c_skipped}\n"
            f"  Metadata extraída: {self.metadata_extracted}\n"
            f"    - GNC: {self.gnc_detected} | 0km: {self.es_0km_detected}\n"
            f"    - Puertas: {self.puertas_extracted} | Tracción: {self.traccion_extracted}\n"
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
        CREATE INDEX IF NOT EXISTS idx_cl_estrategia
            ON {CHANGELOG_TABLE}(estrategia);
    """)
    conn.commit()


def log_change(conn, vid, campo, old, new, estrategia, razon, confidence=0):
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


def ensure_new_columns(conn):
    """Agrega columnas nuevas si no existen (migración segura)."""
    cursor = conn.execute("PRAGMA table_info(vehicles)")
    existing = {row[1] for row in cursor.fetchall()}

    new_columns = {
        'puertas': 'INTEGER',
        'traccion': 'TEXT',
        'tiene_gnc': 'INTEGER DEFAULT 0',
        'es_0km': 'INTEGER DEFAULT 0',
        'version_raw': 'TEXT',
        'descripcion': 'TEXT',
        'norm_confidence': 'INTEGER DEFAULT 0',
    }

    added = []
    for col, col_type in new_columns.items():
        if col not in existing:
            try:
                conn.execute(
                    f"ALTER TABLE vehicles ADD COLUMN {col} {col_type}"
                )
                added.append(col)
            except sqlite3.OperationalError:
                pass

    if added:
        conn.commit()
        logger.info(f"📋 Columnas agregadas: {', '.join(added)}")
    else:
        logger.info("📋 Schema OK (todas las columnas existen)")


def read_all_vehicles(conn):
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, url, marca, modelo, version, año, kilometros,
               norm_status, activo, version_raw, descripcion,
               puertas, traccion, tiene_gnc, es_0km,
               norm_confidence
        FROM vehicles
        ORDER BY id
    """).fetchall()
    return [dict(r) for r in rows]


def read_partial_match_vehicles(conn):
    """Lee vehículos con partial_match para Estrategia C."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, url, marca, modelo, version, año, kilometros,
               norm_status, activo, version_raw, descripcion,
               puertas, traccion, tiene_gnc, es_0km,
               norm_confidence, transmision, combustible
        FROM vehicles
        WHERE norm_status = 'partial_match'
          AND marca IS NOT NULL
          AND modelo IS NOT NULL
        ORDER BY id
    """).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# 📅 AÑO BASURA
# ═══════════════════════════════════════════════════════════════

def is_year_garbage(año, km=None):
    if not año:
        return False, None

    if año > YEAR_MAX_VALID:
        return True, f"Año {año} es futuro (máx: {YEAR_MAX_VALID})"

    if año < YEAR_MIN_VALID:
        return True, f"Año {año} < {YEAR_MIN_VALID}"

    if km and km > 50000 and año >= CURRENT_YEAR:
        return True, (
            f"Año {año} con {km:,} km es imposible"
        )

    return False, None


def is_year_garbage_for_model(año, model_range):
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
    if not año or not marca or not modelo:
        return None

    marca_resolved = catalog.resolve_brand(marca)
    if not catalog.has_brand(marca_resolved):
        return None
    if not catalog.has_model(marca_resolved, modelo):
        return None

    garbage, reason = is_year_garbage(año, km)
    if garbage:
        return {
            'action': 'garbage',
            'suggestion': None,
            'reason': reason,
        }

    yr = catalog.get_year_range(marca_resolved, modelo)
    if not yr:
        return None

    desde = yr.get('desde', 0)
    hasta = yr.get('hasta', 9999)

    if desde <= año <= hasta:
        return None

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

    garbage_model, distance, reason = is_year_garbage_for_model(año, yr)
    if garbage_model:
        return {
            'action': 'garbage',
            'suggestion': None,
            'reason': reason,
        }

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

    prefixes = []
    m = re.match(r'^([a-z]+)[\s\-]?(\d+)', modelo_norm)
    if m:
        prefixes.append(m.group(1))
        prefixes.append(f"{m.group(1)}{m.group(2)}")

    m = re.match(r'^(\d)(\d{2})[a-z]?$', modelo_norm)
    if m:
        prefixes.append(m.group(1))

    m = re.match(r'^(x\d|m\d|z\d|i\d|rs\d|ix\d?)', modelo_norm)
    if m:
        prefixes.append(m.group(1))

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
# 🅲️ ESTRATEGIA C: RE-MATCHING DE VERSIONES (NUEVO v2.0)
# ═══════════════════════════════════════════════════════════════

def extract_metadata_from_text(text: str) -> Dict[str, Any]:
    """Extrae puertas, tracción, GNC, 0km de texto libre."""
    result = {
        'puertas': None,
        'traccion': None,
        'tiene_gnc': None,
        'es_0km': None,
    }
    if not text:
        return result

    t = text.lower()

    # Puertas
    m = re.search(
        r'\b([345])\s*[pP](?:uertas?)?\b', text, re.IGNORECASE
    )
    if m:
        result['puertas'] = int(m.group(1))

    # Tracción ── FIX: (?i) inline → re.IGNORECASE como argumento
    m = re.search(
        r'\b(4x[24])\b|\b(AWD|FWD|RWD)\b', text, re.IGNORECASE
    )
    if m:
        result['traccion'] = (m.group(1) or m.group(2)).lower()

    # GNC
    if re.search(
        r'\bGNC\b|\bgas\s*natural\b|\bc[/\s]?GNC\b|\bcon\s+GNC\b',
        text, re.IGNORECASE
    ):
        result['tiene_gnc'] = True

    # 0km
    if re.search(r'\b0\s*km\b|\bcero\s*km\b|\bokm\b', t):
        result['es_0km'] = True

    return result

def reconstruct_version_input(v: Dict) -> str:
    """Reconstruye un texto de versión a partir de datos existentes."""
    parts = []

    # Usar version_raw si existe
    if v.get('version_raw'):
        return v['version_raw']

    # Reconstruir desde version + datos
    if v.get('version'):
        parts.append(v['version'])

    if v.get('transmision'):
        trans = v['transmision'].lower()
        if trans not in (p.lower() for p in parts):
            parts.append(trans)

    return ' '.join(parts)


def strategy_c_rematch(conn, catalog, dry_run=False):
    """
    Estrategia C: Re-matching de versiones para partial_match.
    """
    logger.info("\n🅲️  ESTRATEGIA C: Re-matching de versiones")
    logger.info("─" * 50)

    vehicles = read_partial_match_vehicles(conn)
    STATS.strategy_c_candidates = len(vehicles)

    if not vehicles:
        logger.info("   Sin candidatos para re-matching")
        return

    logger.info(f"   Candidatos: {len(vehicles)}")

    upgraded = 0
    rematched = 0
    confidence_lowered = 0
    skipped = 0
    examples = []

    for i in range(0, len(vehicles), STRATEGY_C_BATCH_SIZE):
        batch = vehicles[i:i + STRATEGY_C_BATCH_SIZE]

        for v in batch:
            vid = v['id']
            marca = normalize_key(v.get('marca') or '')
            modelo = normalize_key(v.get('modelo') or '')

            if not marca or not modelo:
                skipped += 1
                continue

            marca_resolved = catalog.resolve_brand(marca)
            if not catalog.has_model(marca_resolved, modelo):
                skipped += 1
                continue

            # Reconstruir input de versión
            version_input = reconstruct_version_input(v)
            if not version_input or len(
                version_input.strip()
            ) < 2:
                skipped += 1
                continue

            # Construir texto de búsqueda amplio
            desc = v.get('descripcion') or ''
            search_parts = [version_input]
            if desc:
                search_parts.append(desc[:500])
            search_text = ' '.join(search_parts)

            try:
                # Pre-limpiar marca del version_input
                # usando re.escape para caracteres especiales
                version_for_norm = version_input
                if marca_resolved:
                    try:
                        version_for_norm = re.sub(
                            r'\b' + re.escape(
                                marca_resolved
                            ) + r'\b',
                            '',
                            version_for_norm,
                            flags=re.IGNORECASE
                        ).strip()
                    except re.error:
                        # Si falla el regex, limpiar manualmente
                        version_for_norm = (
                            version_for_norm
                            .replace(marca_resolved, '')
                            .strip()
                        )

                # Usar normalize_vehicle para re-procesar
                norm = nv2.normalize_vehicle(
                    titulo='',
                    descripcion=desc,
                    marca_raw=marca_resolved,
                    modelo_raw=modelo,
                    version_raw=version_for_norm,
                    año_raw=v.get('año'),
                    km_raw=v.get('kilometros'),
                )

                new_version = norm.get('version')
                new_confidence = norm.get('confidence', 0)
                version_method = norm.get('version_method')
                extracted = norm.get('extracted_data', {})

                if new_version:
                    # Verificar coherencia
                    valid_versions = catalog.get_versions(
                        marca_resolved, modelo
                    )
                    version_in_catalog = (
                        normalize_key(new_version) in [
                            normalize_key(vv)
                            for vv in valid_versions
                        ]
                    ) if valid_versions else False

                    if version_in_catalog:
                        rematched += 1

                        if not dry_run:
                            updates = {
                                'version': new_version,
                                'norm_status': 'full_match',
                                'norm_confidence': new_confidence,
                            }

                            # Extraer metadata adicional
                            meta_from_text = (
                                extract_metadata_from_text(
                                    f"{version_input} {desc}"
                                )
                            )

                            if (extracted.get('puertas')
                                    or meta_from_text.get(
                                        'puertas'
                                    )):
                                updates['puertas'] = (
                                    extracted.get('puertas')
                                    or meta_from_text.get(
                                        'puertas'
                                    )
                                )
                                STATS.puertas_extracted += 1

                            if (extracted.get('traccion')
                                    or meta_from_text.get(
                                        'traccion'
                                    )):
                                updates['traccion'] = (
                                    extracted.get('traccion')
                                    or meta_from_text.get(
                                        'traccion'
                                    )
                                )
                                STATS.traccion_extracted += 1

                            if (extracted.get('tiene_gnc')
                                    or meta_from_text.get(
                                        'tiene_gnc'
                                    )):
                                updates['tiene_gnc'] = 1
                                STATS.gnc_detected += 1

                            if (extracted.get('es_0km')
                                    or meta_from_text.get(
                                        'es_0km'
                                    )):
                                updates['es_0km'] = 1
                                STATS.es_0km_detected += 1

                            sets = ', '.join(
                                f"{k} = ?"
                                for k in updates
                            )
                            vals = (
                                list(updates.values()) + [vid]
                            )
                            conn.execute(
                                f"UPDATE vehicles "
                                f"SET {sets} WHERE id = ?",
                                vals
                            )

                            log_change(
                                conn, vid, 'version',
                                v.get('version'),
                                new_version,
                                'C_version_rematch',
                                f"Re-match por {version_method}"
                                f": '{version_input}' → "
                                f"'{new_version}' "
                                f"(conf: {new_confidence})",
                                new_confidence
                            )
                            log_change(
                                conn, vid, 'norm_status',
                                'partial_match', 'full_match',
                                'C_version_rematch',
                                f"Upgrade: versión encontrada "
                                f"por {version_method}",
                                new_confidence
                            )

                        upgraded += 1
                        STATS.total_fixes += 1

                        if len(examples) < 15:
                            examples.append({
                                'id': vid,
                                'marca': marca_resolved,
                                'modelo': modelo,
                                'input': version_input[:60],
                                'version': new_version,
                                'method': version_method,
                                'confidence': new_confidence,
                            })

                    else:
                        # Versión no pertenece al modelo
                        if (new_confidence
                                > STRATEGY_C_MIN_CONFIDENCE):
                            if not dry_run:
                                new_conf = max(
                                    10, new_confidence - 20
                                )
                                conn.execute(
                                    "UPDATE vehicles SET "
                                    "norm_confidence = ? "
                                    "WHERE id = ?",
                                    (new_conf, vid)
                                )
                                log_change(
                                    conn, vid,
                                    'norm_confidence',
                                    str(new_confidence),
                                    str(new_conf),
                                    'C_coherence_check',
                                    f"Versión '{new_version}' "
                                    f"no pertenece a "
                                    f"'{modelo}' según catálogo",
                                    new_conf
                                )
                            confidence_lowered += 1
                else:
                    # No encontró versión, igual extraer metadata
                    meta = extract_metadata_from_text(
                        f"{version_input} {desc}"
                    )
                    meta_updates = {}

                    if (meta.get('puertas')
                            and not v.get('puertas')):
                        meta_updates['puertas'] = (
                            meta['puertas']
                        )
                        STATS.puertas_extracted += 1

                    if (meta.get('traccion')
                            and not v.get('traccion')):
                        meta_updates['traccion'] = (
                            meta['traccion']
                        )
                        STATS.traccion_extracted += 1

                    if (meta.get('tiene_gnc')
                            and not v.get('tiene_gnc')):
                        meta_updates['tiene_gnc'] = 1
                        STATS.gnc_detected += 1

                    km = v.get('kilometros')
                    if (km is not None and km == 0
                            and meta.get('es_0km')
                            and not v.get('es_0km')):
                        meta_updates['es_0km'] = 1
                        STATS.es_0km_detected += 1

                    if meta_updates and not dry_run:
                        sets = ', '.join(
                            f"{k} = ?"
                            for k in meta_updates
                        )
                        vals = (
                            list(meta_updates.values()) + [vid]
                        )
                        conn.execute(
                            f"UPDATE vehicles "
                            f"SET {sets} WHERE id = ?",
                            vals
                        )
                        STATS.metadata_extracted += 1

                    skipped += 1

            except re.error as e:
                # Error de regex: loguear con detalle y continuar
                STATS.errors += 1
                logger.debug(
                    f"  Regex error en {vid} "
                    f"(marca='{marca_resolved}'): {e}"
                )
            except Exception as e:
                STATS.errors += 1
                logger.debug(
                    f"  Error re-matching {vid}: {e}"
                )

        if not dry_run:
            conn.commit()

        processed = min(
            i + STRATEGY_C_BATCH_SIZE, len(vehicles)
        )
        if (processed % 1000 == 0
                or processed == len(vehicles)):
            logger.info(
                f"   {processed}/{len(vehicles)} | "
                f"Upgraded: {upgraded} | "
                f"Rematched: {rematched}"
            )

    STATS.strategy_c_rematched = rematched
    STATS.strategy_c_upgraded = upgraded
    STATS.strategy_c_confidence_lowered = confidence_lowered
    STATS.strategy_c_skipped = skipped

    logger.info(f"\n   📊 Estrategia C completada:")
    logger.info(f"      Candidatos: {len(vehicles)}")
    logger.info(f"      Re-matcheados: {rematched}")
    logger.info(f"      Subidos a full_match: {upgraded}")
    logger.info(
        f"      Confidence bajado: {confidence_lowered}"
    )
    logger.info(f"      Saltados: {skipped}")
    logger.info(
        f"      Metadata extraída: {STATS.metadata_extracted}"
    )

    if examples:
        logger.info(f"\n   🅲️ Ejemplos de re-match:")
        for ex in examples[:10]:
            logger.info(
                f"      [{ex['id']}] "
                f"{ex['marca']} {ex['modelo']}: "
                f"'{ex['input']}' → '{ex['version']}' "
                f"({ex['method']}, conf:{ex['confidence']})"
            )

# ═══════════════════════════════════════════════════════════════
# 🔍 EXTRACCIÓN DE METADATA EN LOTE (para datos existentes)
# ═══════════════════════════════════════════════════════════════

def extract_metadata_batch(conn, dry_run=False):
    """
    Extrae metadata (puertas, tracción, GNC, 0km) de registros
    existentes que tengan version_raw o descripcion pero no
    tienen esos campos completados.
    """
    logger.info("\n🔍 Extracción de metadata en lote")
    logger.info("─" * 50)

    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, version_raw, descripcion, kilometros,
               puertas, traccion, tiene_gnc, es_0km
        FROM vehicles
        WHERE (version_raw IS NOT NULL OR descripcion IS NOT NULL)
          AND (puertas IS NULL AND traccion IS NULL
               AND tiene_gnc = 0 AND es_0km = 0)
    """).fetchall()

    if not rows:
        logger.info("   Sin registros pendientes de extracción")
        return

    logger.info(f"   Registros a procesar: {len(rows)}")
    updated = 0

    for row in rows:
        row = dict(row)
        text = ' '.join(filter(None, [
            row.get('version_raw', ''),
            row.get('descripcion', '')
        ]))

        if not text.strip():
            continue

        meta = extract_metadata_from_text(text)
        updates = {}

        if meta.get('puertas') and not row.get('puertas'):
            updates['puertas'] = meta['puertas']
            STATS.puertas_extracted += 1

        if meta.get('traccion') and not row.get('traccion'):
            updates['traccion'] = meta['traccion']
            STATS.traccion_extracted += 1

        if meta.get('tiene_gnc') and not row.get('tiene_gnc'):
            updates['tiene_gnc'] = 1
            STATS.gnc_detected += 1

        km = row.get('kilometros')
        if (km is not None and km == 0 and
                meta.get('es_0km') and not row.get('es_0km')):
            updates['es_0km'] = 1
            STATS.es_0km_detected += 1

        if updates and not dry_run:
            sets = ', '.join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [row['id']]
            conn.execute(
                f"UPDATE vehicles SET {sets} WHERE id = ?", vals
            )
            updated += 1

    if not dry_run:
        conn.commit()

    logger.info(f"   Actualizados: {updated}")
    STATS.metadata_extracted += updated


# ═══════════════════════════════════════════════════════════════
# 🔄 PROCESO PRINCIPAL (Estrategias A + B)
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
            'campo': 'marca',
            'old': marca,
            'new': marca_resolved,
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
                'campo': 'año',
                'old': año,
                'new': None,
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
            'campo': 'modelo',
            'old': modelo,
            'new': mapped,
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
                    'campo': 'año',
                    'old': año,
                    'new': None,
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
                change['estrategia'],
                change['reason'],
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
                dicts_path='config/normalizer_dicts.json',
                skip_c=False, only_c=False):
    global STATS
    STATS = CleanerStats()

    logger.info("🧹 " + "=" * 58)
    logger.info("🧹 DB CLEANER v2.0 — Estrategias A + B + C")
    logger.info("🧹 " + "=" * 58)
    logger.info(
        f"   Tolerancia de año: ±{YEAR_TOLERANCE} | "
        f"Umbral basura: ±{YEAR_GARBAGE_THRESHOLD}"
    )
    if skip_c:
        logger.info("   ⏭️ Estrategia C desactivada")
    if only_c:
        logger.info("   🎯 Solo Estrategia C")
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

    # Migrar schema
    ensure_new_columns(conn)
    setup_changelog(conn)

    # ── Estrategias A + B ──
    if not only_c:
        vehicles = read_all_vehicles(conn)
        STATS.total_vehicles = len(vehicles)
        logger.info(f"📊 Vehículos: {len(vehicles)}")

        examples_a = []
        examples_b = []
        examples_garbage = []

        batch_size = 500
        for i in range(0, len(vehicles), batch_size):
            batch = vehicles[i:i + batch_size]
            for v in batch:
                STATS.analyzed += 1
                try:
                    changes = process_vehicle(
                        v, catalog, conn, dry_run
                    )
                    for c in changes:
                        ex = {
                            'id': v['id'],
                            'año': v.get('año'),
                            **c
                        }
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

    # ── Estrategia C ──
    if not skip_c:
        strategy_c_rematch(conn, catalog, dry_run)

    # ── Extracción de metadata en lote ──
    extract_metadata_batch(conn, dry_run)

    if not dry_run:
        conn.commit()

    # ── Reporte ──
    logger.info(f"\n{'═' * 60}")
    logger.info("📊 REPORTE DE LIMPIEZA")
    logger.info(f"{'═' * 60}")
    logger.info(f"\n{STATS.summary()}")

    if not only_c:
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
                logger.info(
                    f"      ↳ {ex['reason']}"
                )

    # Estado final
    if not dry_run:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            "SELECT COUNT(*) as c FROM vehicles"
        ).fetchone()['c']
        logger.info(f"\n📋 BD final: {total} vehículos")

        rows = conn.execute("""
            SELECT norm_status, COUNT(*) as c
            FROM vehicles
            GROUP BY norm_status
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

        # Estadísticas Estrategia C
        try:
            c_entries = conn.execute(
                f"SELECT COUNT(*) as c FROM {CHANGELOG_TABLE} "
                f"WHERE estrategia LIKE 'C_%'"
            ).fetchone()['c']
            if c_entries:
                logger.info(
                    f"   └─ Estrategia C: {c_entries} cambios"
                )
        except:
            pass

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
        description='🧹 Limpieza de BD v2.0 — Estrategias A + B + C'
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
        '--dicts-path',
        default='config/normalizer_dicts.json',
        help='Ruta al JSON de diccionarios'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Más logs'
    )
    parser.add_argument(
        '--skip-c', action='store_true',
        help='Saltar Estrategia C (re-matching versiones)'
    )
    parser.add_argument(
        '--only-c', action='store_true',
        help='Ejecutar SOLO Estrategia C'
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)
    stats = run_cleaner(
        db_path=args.db,
        dry_run=args.dry_run,
        dicts_path=args.dicts_path,
        skip_c=args.skip_c,
        only_c=args.only_c,
    )

    # El error de exit solo aplica a errores CRÍTICOS,
    # no a los de re-matching (que son normales cuando
    # el normalizer no puede procesar ciertos registros)
    critical_errors = max(
        0, stats.errors - stats.strategy_c_candidates
    )
    threshold = max(stats.total_vehicles * 0.1, 100)
    if critical_errors > threshold:
        logger.error(
            f"❌ Demasiados errores críticos: "
            f"{critical_errors} > {threshold}"
        )
        sys.exit(1)

    sys.exit(0)

if __name__ == "__main__":
    main()
