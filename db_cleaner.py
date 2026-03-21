"""
🧹 db_cleaner.py - Corrección de BD con Estrategias A y B
==========================================================

Lee rosariogarage.db y corrige errores de normalización usando
normalizer_v2 SIN necesidad de red (datos ya existentes en BD).

ESTRATEGIA A: Validación por año
  - Si marca+modelo+año existen pero el año cae fuera del rango
    de producción del modelo → busca sucesor/predecesor en succession_map
  - Ejemplo: Peugeot 206 año 2015 → corrige a 208

ESTRATEGIA B: Mapeo de códigos atrapados como modelo
  - Si el modelo es un código (c200, 320i, gla200) → mapea al nombre
    de catálogo (clase c, serie 3, clase gla)
  - Ejemplo: Mercedes-Benz modelo="c200" → modelo="clase c"

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
import json
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from collections import Counter

# ═══════════════════════════════════════════════════════════════
# 📦 IMPORT NORMALIZER V2
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

    # Estrategia B
    code_issues_found: int = 0
    code_fixes_applied: int = 0

    # Alias de marca
    marca_alias_fixed: int = 0

    # General
    total_fixes: int = 0
    skipped: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"Analizados: {self.analyzed}/{self.total_vehicles}\n"
            f"  Estrategia A (año→modelo): {self.year_fixes_applied} fixes "
            f"({self.year_issues_found} detectados, "
            f"{self.year_no_suggestion} sin sugerencia)\n"
            f"  Estrategia B (código→nombre): {self.code_fixes_applied} fixes "
            f"({self.code_issues_found} detectados)\n"
            f"  Alias de marca: {self.marca_alias_fixed}\n"
            f"  Total fixes aplicados: {self.total_fixes}\n"
            f"  Errores: {self.errors}"
        )


STATS = CleanerStats()

# ═══════════════════════════════════════════════════════════════
# 🔧 LOGGING
# ═══════════════════════════════════════════════════════════════

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s │ %(levelname)-7s │ %(message)s',
        datefmt='%H:%M:%S'
    )
    return logging.getLogger(__name__)


logger = setup_logging()

# ═══════════════════════════════════════════════════════════════
# 🛠️ HELPERS
# ═══════════════════════════════════════════════════════════════

def create_backup(db_path: str) -> str:
    """
    Copia la BD original a rosariogarage_nofix.db
    """
    base, ext = os.path.splitext(db_path)
    backup_path = f"{base}{BACKUP_SUFFIX}{ext}"
    shutil.copy2(db_path, backup_path)
    size_mb = os.path.getsize(backup_path) / (1024 * 1024)
    logger.info(f"💾 Backup creado: {backup_path} ({size_mb:.1f} MB)")
    return backup_path


def setup_changelog(conn: sqlite3.Connection):
    """Crea tabla de changelog para auditoría."""
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


def log_change(conn: sqlite3.Connection, vehicle_id: str, campo: str,
               old_val: Any, new_val: Any, estrategia: str,
               razon: str, confidence: int = 0):
    """Registra un cambio en el changelog."""
    conn.execute(f"""
        INSERT INTO {CHANGELOG_TABLE}
        (vehicle_id, campo, valor_anterior, valor_nuevo,
         estrategia, razon, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        vehicle_id, campo,
        str(old_val) if old_val else None,
        str(new_val) if new_val else None,
        estrategia, razon, confidence
    ))


def read_all_vehicles(conn: sqlite3.Connection) -> List[Dict]:
    """Lee todos los vehículos de la BD."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, url, marca, modelo, version, año,
               kilometros, norm_status, activo
        FROM vehicles
        ORDER BY id
    """).fetchall()
    return [dict(r) for r in rows]


def normalize_key(text: str) -> str:
    """Normaliza texto para comparación."""
    if not text:
        return ""
    return text.lower().strip()

# ═══════════════════════════════════════════════════════════════
# 🅰️ ESTRATEGIA A: VALIDACIÓN POR AÑO
# ═══════════════════════════════════════════════════════════════

def check_year_range(catalog, marca: str, modelo: str, año: int
                     ) -> Optional[Dict]:
    """
    Verifica si el año está dentro del rango del modelo.

    Returns:
        None si está OK o no hay datos para verificar
        Dict con 'suggestion', 'desde', 'hasta', 'razon' si hay problema
    """
    if not año or not marca or not modelo:
        return None

    marca_resolved = catalog.resolve_brand(marca)

    # Verificar que la marca y modelo existen en catálogo
    if not catalog.has_brand(marca_resolved):
        return None
    if not catalog.has_model(marca_resolved, modelo):
        return None

    # Obtener rango de años
    yr = catalog.get_year_range(marca_resolved, modelo)
    if not yr:
        return None

    desde = yr.get('desde', 0)
    hasta = yr.get('hasta', 9999)

    # Si el año está dentro del rango, todo OK
    if desde <= año <= hasta:
        return None

    # ── Año fuera de rango → buscar sugerencia ──
    result = {
        'desde': desde,
        'hasta': hasta,
        'suggestion': None,
        'suggestion_range': None,
        'razon': None,
    }

    # Intentar con get_year_suggestion del catálogo
    suggestion = catalog.get_year_suggestion(marca_resolved, modelo, año)

    if suggestion:
        # Verificar que la sugerencia existe y cubre el año
        if catalog.has_model(marca_resolved, suggestion):
            sug_yr = catalog.get_year_range(marca_resolved, suggestion)
            if sug_yr:
                sug_desde = sug_yr.get('desde', 0)
                sug_hasta = sug_yr.get('hasta', 9999)
                if sug_desde <= año <= sug_hasta:
                    result['suggestion'] = suggestion
                    result['suggestion_range'] = (sug_desde, sug_hasta)
                    result['razon'] = (
                        f"'{modelo}' existió {desde}-{hasta}, "
                        f"año {año} corresponde a '{suggestion}' "
                        f"({sug_desde}-{sug_hasta})"
                    )
                    return result

    # Intentar succession_map directamente
    if año > hasta:
        # Año posterior → buscar sucesor
        sucesor = catalog.get_successor(marca_resolved, modelo)
        if sucesor and catalog.has_model(marca_resolved, sucesor):
            suc_yr = catalog.get_year_range(marca_resolved, sucesor)
            if suc_yr:
                suc_desde = suc_yr.get('desde', 0)
                suc_hasta = suc_yr.get('hasta', 9999)
                if suc_desde <= año <= suc_hasta:
                    result['suggestion'] = sucesor
                    result['suggestion_range'] = (suc_desde, suc_hasta)
                    result['razon'] = (
                        f"'{modelo}' existió {desde}-{hasta}, "
                        f"año {año} → sucesor '{sucesor}' "
                        f"({suc_desde}-{suc_hasta})"
                    )
                    return result

                # Quizás hay un sucesor del sucesor
                sucesor2 = catalog.get_successor(marca_resolved, sucesor)
                if sucesor2 and catalog.has_model(marca_resolved, sucesor2):
                    suc2_yr = catalog.get_year_range(
                        marca_resolved, sucesor2
                    )
                    if suc2_yr:
                        s2d = suc2_yr.get('desde', 0)
                        s2h = suc2_yr.get('hasta', 9999)
                        if s2d <= año <= s2h:
                            result['suggestion'] = sucesor2
                            result['suggestion_range'] = (s2d, s2h)
                            result['razon'] = (
                                f"'{modelo}' existió {desde}-{hasta}, "
                                f"año {año} → sucesor² '{sucesor2}' "
                                f"({s2d}-{s2h})"
                            )
                            return result

    elif año < desde:
        # Año anterior → buscar predecesor
        predecesor = catalog.get_predecessor(marca_resolved, modelo)
        if predecesor and catalog.has_model(marca_resolved, predecesor):
            pred_yr = catalog.get_year_range(marca_resolved, predecesor)
            if pred_yr:
                pred_desde = pred_yr.get('desde', 0)
                pred_hasta = pred_yr.get('hasta', 9999)
                if pred_desde <= año <= pred_hasta:
                    result['suggestion'] = predecesor
                    result['suggestion_range'] = (pred_desde, pred_hasta)
                    result['razon'] = (
                        f"'{modelo}' existió {desde}-{hasta}, "
                        f"año {año} → predecesor '{predecesor}' "
                        f"({pred_desde}-{pred_hasta})"
                    )
                    return result

    # Año fuera de rango pero sin sugerencia válida
    result['razon'] = (
        f"'{modelo}' existió {desde}-{hasta}, "
        f"año {año} fuera de rango, sin sugerencia encontrada"
    )
    return result

# ═══════════════════════════════════════════════════════════════
# 🅱️ ESTRATEGIA B: MAPEO DE CÓDIGOS
# ═══════════════════════════════════════════════════════════════

def check_code_mapping(catalog, marca: str, modelo: str
                       ) -> Optional[Dict]:
    """
    Verifica si el modelo actual es un código que debería
    mapearse a un nombre de catálogo.

    Ejemplo: marca="mercedes-benz", modelo="c200"
             → modelo debería ser "clase c"

    Returns:
        None si no hay mapeo necesario
        Dict con 'mapped_model', 'razon' si hay corrección
    """
    if not marca or not modelo:
        return None

    marca_resolved = catalog.resolve_brand(marca)
    modelo_norm = normalize_key(modelo)

    # Si el modelo YA existe en el catálogo, no tocar
    if catalog.has_model(marca_resolved, modelo_norm):
        return None

    # Obtener code_mappings para esta marca
    code_mappings = catalog.code_mappings.get(
        normalize_key(marca_resolved), {}
    )
    if not code_mappings:
        return None

    # ── Intentar match exacto del modelo como código ──
    if modelo_norm in code_mappings:
        mapped = code_mappings[modelo_norm]
        # Resolver si es dict (ambiguo) o string
        if isinstance(mapped, dict):
            if mapped.get('ambiguo'):
                # No corregir si es ambiguo
                return None
            mapped_model = mapped.get('modelos', [None])[0]
        else:
            mapped_model = mapped

        if mapped_model and catalog.has_model(marca_resolved, mapped_model):
            return {
                'mapped_model': mapped_model,
                'razon': (
                    f"Código '{modelo}' mapeado a '{mapped_model}' "
                    f"en catálogo de {marca_resolved}"
                )
            }

    # ── Intentar extraer prefijo del modelo ──
    # Ej: "c200" → prefijo "c", "gla200" → prefijo "gla"
    # Ej: "320i" → prefijo "3"
    prefixes_to_try = []

    # Patrón: letras + números (c200, gla250, cls350)
    m = re.match(r'^([a-z]+)[\s\-]?(\d+)', modelo_norm)
    if m:
        prefixes_to_try.append(m.group(1))
        # También intentar con el número completo como código
        prefixes_to_try.append(f"{m.group(1)}{m.group(2)}")

    # Patrón: número + letra (320i, 520d)
    m = re.match(r'^(\d)(\d{2})[a-z]?$', modelo_norm)
    if m:
        prefixes_to_try.append(m.group(1))

    # Patrón: x + número (x1, x3, x5)
    m = re.match(r'^(x\d|m\d|z\d|i\d|rs\d|ix\d?)', modelo_norm)
    if m:
        prefixes_to_try.append(m.group(1))

    # Patrón: a1, a3, q5, s3, tt, etc
    m = re.match(r'^([aqst]{1,2}\d?)', modelo_norm)
    if m:
        prefixes_to_try.append(m.group(1))

    # Intentar cada prefijo
    for prefix in prefixes_to_try:
        if prefix in code_mappings:
            mapped = code_mappings[prefix]
            if isinstance(mapped, dict):
                if mapped.get('ambiguo'):
                    continue
                mapped_model = mapped.get('modelos', [None])[0]
            else:
                mapped_model = mapped

            if (mapped_model
                    and catalog.has_model(marca_resolved, mapped_model)):
                return {
                    'mapped_model': mapped_model,
                    'razon': (
                        f"Código '{modelo}' (prefijo '{prefix}') "
                        f"mapeado a '{mapped_model}'"
                    )
                }

    return None

# ═══════════════════════════════════════════════════════════════
# 🔄 PROCESO PRINCIPAL
# ═══════════════════════════════════════════════════════════════

def process_vehicle(v: Dict, catalog, conn: sqlite3.Connection,
                    dry_run: bool = False) -> List[Dict]:
    """
    Procesa un vehículo aplicando Estrategias A y B.
    Retorna lista de cambios aplicados.
    """
    vid = v['id']
    marca = normalize_key(v.get('marca') or '')
    modelo = normalize_key(v.get('modelo') or '')
    version = v.get('version') or ''
    año = v.get('año')
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
            'razon': f"Alias de marca: '{marca}' → '{marca_resolved}'",
            'confidence': 95,
        })
        marca = marca_resolved
        STATS.marca_alias_fixed += 1

    if not modelo:
        return changes

    # ── ESTRATEGIA B: Mapeo de código (primero) ──
    # Se ejecuta ANTES de A porque si el modelo es un código,
    # primero lo mapeamos y luego validamos el año
    code_result = check_code_mapping(catalog, marca, modelo)
    if code_result:
        STATS.code_issues_found += 1
        mapped = code_result['mapped_model']
        changes.append({
            'campo': 'modelo',
            'old': modelo,
            'new': mapped,
            'estrategia': 'B_code_mapping',
            'razon': code_result['razon'],
            'confidence': 90,
        })
        # Actualizar modelo para que la Estrategia A use el correcto
        modelo = mapped
        STATS.code_fixes_applied += 1

    # ── ESTRATEGIA A: Validación por año ──
    if año:
        year_result = check_year_range(catalog, marca, modelo, año)
        if year_result:
            STATS.year_issues_found += 1
            if year_result.get('suggestion'):
                suggestion = year_result['suggestion']
                changes.append({
                    'campo': 'modelo',
                    'old': modelo,
                    'new': suggestion,
                    'estrategia': 'A_year_validation',
                    'razon': year_result['razon'],
                    'confidence': 85,
                })
                STATS.year_fixes_applied += 1
            else:
                STATS.year_no_suggestion += 1
                logger.debug(
                    f"  ⚠️ [{vid}] Año fuera de rango sin sugerencia: "
                    f"{year_result['razon']}"
                )

    # ── Aplicar cambios ──
    if changes and not dry_run:
        for change in changes:
            # Si hay múltiples cambios al mismo campo, usar el último
            pass

        # Consolidar: tomar el último valor de cada campo
        final_values = {}
        for change in changes:
            campo = change['campo']
            final_values[campo] = change['new']
            log_change(
                conn, vid, campo,
                change['old'], change['new'],
                change['estrategia'], change['razon'],
                change['confidence']
            )

        # Determinar nuevo norm_status
        new_marca = final_values.get('marca', marca)
        new_modelo = final_values.get('modelo', modelo)

        if catalog.has_model(new_marca, new_modelo):
            versions = catalog.get_versions(new_marca, new_modelo)
            # Verificar si la versión actual matchea
            version_norm = normalize_key(version)
            has_version = version_norm in [
                normalize_key(v) for v in versions
            ] if versions else False
            new_status = 'full_match' if has_version else 'partial_match'
        else:
            new_status = 'fallback'

        final_values['norm_status'] = new_status

        # Ejecutar UPDATE
        sets = ', '.join(f"{k} = ?" for k in final_values)
        vals = list(final_values.values()) + [vid]
        conn.execute(
            f"UPDATE vehicles SET {sets} WHERE id = ?", vals
        )

    return changes


def run_cleaner(db_path: str, dry_run: bool = False,
                dicts_path: str = 'config/normalizer_dicts.json'):
    """Ejecuta el proceso completo de limpieza."""
    global STATS
    STATS = CleanerStats()

    logger.info("🧹 " + "=" * 58)
    logger.info("🧹 DB CLEANER - Estrategias A + B")
    logger.info("🧹 " + "=" * 58)

    if dry_run:
        logger.info("⚡ MODO DRY-RUN: No se aplicarán cambios")

    # ── Verificar BD ──
    if not os.path.exists(db_path):
        logger.error(f"❌ BD no encontrada: {db_path}")
        sys.exit(1)

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    logger.info(f"📂 BD: {db_path} ({size_mb:.1f} MB)")

    # ── Verificar normalizer_v2 ──
    if not HAS_NV2:
        logger.error("❌ normalizer_v2.py no encontrado")
        sys.exit(1)

    # ── Inicializar catálogo ──
    loaded = nv2.init_normalizer(dicts_path=dicts_path)
    if not loaded:
        logger.error(f"❌ No se pudo cargar catálogo: {dicts_path}")
        sys.exit(1)

    catalog = nv2.get_catalog()
    logger.info("✅ Catálogo cargado")

    # ── Backup ──
    if not dry_run:
        backup_path = create_backup(db_path)

    # ── Conectar BD ──
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    setup_changelog(conn)

    # ── Leer vehículos ──
    vehicles = read_all_vehicles(conn)
    STATS.total_vehicles = len(vehicles)
    logger.info(f"📊 Vehículos en BD: {len(vehicles)}")

    # ── Contar estado actual ──
    with_marca = sum(1 for v in vehicles if v.get('marca'))
    with_modelo = sum(1 for v in vehicles if v.get('modelo'))
    with_year = sum(1 for v in vehicles if v.get('año'))

    logger.info(f"   Con marca: {with_marca}")
    logger.info(f"   Con modelo: {with_modelo}")
    logger.info(f"   Con año: {with_year}")

    # ── Procesar ──
    logger.info(f"\n{'─' * 60}")
    logger.info("🔄 PROCESANDO VEHÍCULOS...")
    logger.info(f"{'─' * 60}")

    all_changes = []
    batch_size = 500
    examples_a = []
    examples_b = []
    examples_alias = []

    for i in range(0, len(vehicles), batch_size):
        batch = vehicles[i:i + batch_size]

        for v in batch:
            STATS.analyzed += 1
            try:
                changes = process_vehicle(v, catalog, conn, dry_run)
                if changes:
                    STATS.total_fixes += len(changes)
                    all_changes.extend(
                        [{'vehicle_id': v['id'], **c} for c in changes]
                    )

                    # Guardar ejemplos
                    for c in changes:
                        example = {
                            'id': v['id'],
                            'año': v.get('año'),
                            **c
                        }
                        if (c['estrategia'] == 'A_year_validation'
                                and len(examples_a) < 10):
                            examples_a.append(example)
                        elif (c['estrategia'] == 'B_code_mapping'
                                and len(examples_b) < 10):
                            examples_b.append(example)
                        elif (c['estrategia'] == 'alias_marca'
                                and len(examples_alias) < 5):
                            examples_alias.append(example)

            except Exception as e:
                STATS.errors += 1
                logger.debug(f"Error procesando {v['id']}: {e}")

        if not dry_run:
            conn.commit()

        processed = min(i + batch_size, len(vehicles))
        fixes_so_far = STATS.total_fixes
        logger.info(
            f"   {processed}/{len(vehicles)} | Fixes: {fixes_so_far}"
        )

    if not dry_run:
        conn.commit()

    # ═══════════════════════════════════════════════════════
    # 📊 REPORTE
    # ═══════════════════════════════════════════════════════

    logger.info(f"\n{'═' * 60}")
    logger.info("📊 REPORTE DE LIMPIEZA")
    logger.info(f"{'═' * 60}")
    logger.info(f"\n{STATS.summary()}")

    if examples_alias:
        logger.info(f"\n🏷️ Ejemplos - Alias de marca:")
        for ex in examples_alias:
            logger.info(
                f"   [{ex['id']}] {ex['old']} → {ex['new']}"
            )

    if examples_b:
        logger.info(f"\n🅱️ Ejemplos - Código → nombre de catálogo:")
        for ex in examples_b:
            logger.info(
                f"   [{ex['id']}] {ex['old']} → {ex['new']} "
                f"({ex['razon']})"
            )

    if examples_a:
        logger.info(f"\n🅰️ Ejemplos - Corrección por año:")
        for ex in examples_a:
            logger.info(
                f"   [{ex['id']}] año {ex.get('año')}: "
                f"{ex['old']} → {ex['new']}"
            )
            logger.info(f"      ↳ {ex['razon']}")

    # ── Estado final de la BD ──
    if not dry_run:
        logger.info(f"\n{'─' * 60}")
        logger.info("📋 ESTADO FINAL DE LA BD")
        logger.info(f"{'─' * 60}")

        conn.row_factory = sqlite3.Row

        total = conn.execute(
            "SELECT COUNT(*) as c FROM vehicles"
        ).fetchone()['c']
        activos = conn.execute(
            "SELECT COUNT(*) as c FROM vehicles WHERE activo = 1"
        ).fetchone()['c']

        logger.info(f"   Total: {total} | Activos: {activos}")

        # Distribución norm_status
        rows = conn.execute("""
            SELECT norm_status, COUNT(*) as c
            FROM vehicles
            GROUP BY norm_status
            ORDER BY c DESC
        """).fetchall()
        logger.info(f"\n   Distribución norm_status:")
        for row in rows:
            logger.info(f"      {row['norm_status'] or 'NULL'}: {row['c']}")

        # Changelog
        cl_count = conn.execute(
            f"SELECT COUNT(*) as c FROM {CHANGELOG_TABLE}"
        ).fetchone()['c']
        logger.info(f"\n   Entradas en changelog: {cl_count}")

        if cl_count > 0:
            rows = conn.execute(f"""
                SELECT estrategia, COUNT(*) as c
                FROM {CHANGELOG_TABLE}
                GROUP BY estrategia
                ORDER BY c DESC
            """).fetchall()
            for row in rows:
                logger.info(
                    f"      {row['estrategia']}: {row['c']}"
                )

    conn.close()

    if dry_run:
        logger.info(f"\n⚡ DRY-RUN: {STATS.total_fixes} cambios "
                     f"identificados, ninguno aplicado")

    logger.info(f"\n{'═' * 60}")
    logger.info("✅ PROCESO COMPLETADO")
    logger.info(f"{'═' * 60}")

    return STATS


# ═══════════════════════════════════════════════════════════════
# 🚀 MAIN
# ═══════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description='🧹 Limpieza de BD - Estrategias A y B'
    )
    parser.add_argument(
        '--db', default=DEFAULT_DB,
        help=f'Ruta a la BD (default: {DEFAULT_DB})'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Simular cambios sin aplicarlos'
    )
    parser.add_argument(
        '--dicts-path', default='config/normalizer_dicts.json',
        help='Ruta al JSON de diccionarios del normalizer v2'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Más logs'
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.verbose:
        setup_logging(verbose=True)

    stats = run_cleaner(
        db_path=args.db,
        dry_run=args.dry_run,
        dicts_path=args.dicts_path,
    )

    # Exit code basado en errores
    if stats.errors > stats.total_vehicles * 0.1:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
