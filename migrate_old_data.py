"""
🔄 migrate_old_data.py v1.1 - Migración retroactiva de datos existentes
=========================================================================
Aplica las mejoras v2.6 a los registros viejos de la BD:

  PASO 1: Agregar columnas nuevas al schema (SIEMPRE, incluso en dry-run)
  PASO 2: Poblar version_raw desde version existente
  PASO 3: Extraer metadata (puertas, tracción, GNC, 0km) de textos
  PASO 4: Re-normalizar registros parciales/fallback con normalizer v5.0
  PASO 5: Estadísticas finales y comparación

NOTA: dry-run solo afecta modificaciones de DATOS (pasos 2-4).
      Las columnas (paso 1) se agregan siempre porque son necesarias
      para que los pasos siguientes puedan consultar la BD.

EJECUCIÓN DESDE GITHUB ACTIONS:
  python migrate_old_data.py
  python migrate_old_data.py --dry-run
  python migrate_old_data.py --db rosariogarage.db --verbose
"""

import sqlite3
import sys
import os
import re
import json
import logging
import shutil
import argparse
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════════
# 📦 IMPORTS OPCIONALES
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
DICTS_PATH = 'config/normalizer_dicts.json'
BRAND_ALIASES_PATH = 'config/brand_aliases.json'

RE_PUERTAS = re.compile(r'\b([345])\s*[pP](?:uertas?)?\b')
RE_TRACCION = re.compile(
    r'\b(4x[24])\b|\b(AWD|FWD|RWD)\b', re.IGNORECASE
)
RE_GNC = re.compile(
    r'\bGNC\b|\bgas\s*natural\b|\bc[/\s]?GNC\b|\bcon\s+GNC\b',
    re.IGNORECASE
)
RE_0KM = re.compile(
    r'\b0\s*km\b|\bcero\s*km\b|\bokm\b', re.IGNORECASE
)

# Columnas nuevas v2.6
NEW_COLUMNS = {
    'puertas': 'INTEGER',
    'traccion': 'TEXT',
    'tiene_gnc': 'INTEGER DEFAULT 0',
    'es_0km': 'INTEGER DEFAULT 0',
    'version_raw': 'TEXT',
    'descripcion': 'TEXT',
    'norm_confidence': 'INTEGER DEFAULT 0',
}

# Status que se pueden re-normalizar
RENORM_STATUSES = (
    'partial_match', 'fallback', 'pending',
    'unknown', 'no_modelo', 'error', 'raw',
)

# Ranking de status (mayor = mejor)
STATUS_RANK = {
    'full_match': 5,
    'partial_match': 4,
    'marca_only': 3,
    'fallback': 2,
    'pending': 1,
    'unknown': 0,
    'no_modelo': 0,
    'error': 0,
    'raw': 0,
    'no_marca': 0,
}

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 📊 ESTADÍSTICAS
# ═══════════════════════════════════════════════════════════════

@dataclass
class MigrationStats:
    # Paso 1
    columns_added: int = 0
    columns_existed: int = 0

    # Paso 2
    version_raw_populated: int = 0
    version_raw_already: int = 0

    # Paso 3
    metadata_processed: int = 0
    puertas_extracted: int = 0
    traccion_extracted: int = 0
    gnc_detected: int = 0
    es_0km_detected: int = 0
    metadata_updated: int = 0

    # Paso 4
    renorm_candidates: int = 0
    renorm_version_found: int = 0
    renorm_model_improved: int = 0
    renorm_status_upgraded: int = 0
    renorm_metadata_added: int = 0
    renorm_errors: int = 0

    # General
    total_vehicles: int = 0
    total_changes: int = 0

    def summary(self) -> str:
        return (
            f"{'═' * 60}\n"
            f"📊 RESUMEN DE MIGRACIÓN\n"
            f"{'═' * 60}\n"
            f"Total vehículos: {self.total_vehicles}\n"
            f"Total cambios: {self.total_changes}\n"
            f"\n"
            f"Paso 1 - Schema:\n"
            f"  Columnas agregadas: {self.columns_added}\n"
            f"  Ya existían: {self.columns_existed}\n"
            f"\n"
            f"Paso 2 - version_raw:\n"
            f"  Poblados: {self.version_raw_populated}\n"
            f"  Ya tenían: {self.version_raw_already}\n"
            f"\n"
            f"Paso 3 - Metadata:\n"
            f"  Procesados: {self.metadata_processed}\n"
            f"  Actualizados: {self.metadata_updated}\n"
            f"  ├─ Puertas: {self.puertas_extracted}\n"
            f"  ├─ Tracción: {self.traccion_extracted}\n"
            f"  ├─ GNC: {self.gnc_detected}\n"
            f"  └─ 0km: {self.es_0km_detected}\n"
            f"\n"
            f"Paso 4 - Re-normalización:\n"
            f"  Candidatos: {self.renorm_candidates}\n"
            f"  Versiones encontradas: {self.renorm_version_found}\n"
            f"  Modelos mejorados: {self.renorm_model_improved}\n"
            f"  Status upgrades: {self.renorm_status_upgraded}\n"
            f"  Metadata agregada: {self.renorm_metadata_added}\n"
            f"  Errores: {self.renorm_errors}\n"
            f"{'═' * 60}"
        )


STATS = MigrationStats()


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


def get_existing_columns(conn) -> set:
    """Obtiene set de columnas existentes en vehicles."""
    cursor = conn.execute("PRAGMA table_info(vehicles)")
    return {row[1] for row in cursor.fetchall()}


def get_norm_status_counts(conn) -> Dict[str, int]:
    """Obtiene conteo por norm_status."""
    rows = conn.execute("""
        SELECT COALESCE(norm_status, 'NULL') as status,
               COUNT(*) as c
        FROM vehicles
        GROUP BY norm_status
        ORDER BY c DESC
    """).fetchall()
    return {r[0]: r[1] for r in rows}


def get_full_stats(conn) -> Dict[str, Any]:
    """Obtiene estadísticas completas de la BD."""
    stats = {}

    stats['total'] = conn.execute(
        "SELECT COUNT(*) FROM vehicles"
    ).fetchone()[0]

    stats['activos'] = conn.execute(
        "SELECT COUNT(*) FROM vehicles WHERE activo = 1"
    ).fetchone()[0]

    stats['con_marca'] = conn.execute(
        "SELECT COUNT(*) FROM vehicles "
        "WHERE marca IS NOT NULL AND marca != ''"
    ).fetchone()[0]

    stats['con_modelo'] = conn.execute(
        "SELECT COUNT(*) FROM vehicles "
        "WHERE modelo IS NOT NULL AND modelo != ''"
    ).fetchone()[0]

    try:
        stats['con_version'] = conn.execute(
            "SELECT COUNT(*) FROM vehicles "
            "WHERE version IS NOT NULL AND version != ''"
        ).fetchone()[0]
    except Exception:
        stats['con_version'] = 0

    stats['norm_status'] = get_norm_status_counts(conn)

    # Metadata — consultar solo columnas que existen
    existing = get_existing_columns(conn)

    for col in ['puertas', 'traccion', 'tiene_gnc',
                'es_0km', 'version_raw', 'descripcion']:
        key = f'con_{col}'
        if col not in existing:
            stats[key] = 0
            continue
        try:
            if col in ('tiene_gnc', 'es_0km'):
                stats[key] = conn.execute(
                    f"SELECT COUNT(*) FROM vehicles "
                    f"WHERE {col} = 1"
                ).fetchone()[0]
            else:
                stats[key] = conn.execute(
                    f"SELECT COUNT(*) FROM vehicles "
                    f"WHERE {col} IS NOT NULL"
                ).fetchone()[0]
        except Exception:
            stats[key] = 0

    return stats


def print_comparison(antes: Dict, despues: Dict):
    """Imprime tabla comparativa ANTES vs DESPUÉS."""
    print(f"\n{'═' * 65}")
    print("📊 COMPARACIÓN ANTES vs DESPUÉS")
    print(f"{'═' * 65}")

    print(
        f"\n{'Métrica':<30} {'Antes':>10} "
        f"{'Después':>10} {'Δ':>10}"
    )
    print("─" * 65)

    for key, label in [
        ('total', 'Total vehículos'),
        ('activos', 'Activos'),
        ('con_marca', 'Con marca'),
        ('con_modelo', 'Con modelo'),
        ('con_version', 'Con versión'),
    ]:
        a = antes.get(key, 0)
        d = despues.get(key, 0)
        delta = d - a
        sign = '+' if delta > 0 else ''
        print(f"  {label:<28} {a:>10} {d:>10} {sign}{delta:>9}")

    print(f"\n  Metadata:")
    for key, label in [
        ('con_puertas', 'Con puertas'),
        ('con_traccion', 'Con tracción'),
        ('con_tiene_gnc', 'Con GNC'),
        ('con_es_0km', '0km'),
        ('con_version_raw', 'Con version_raw'),
        ('con_descripcion', 'Con descripción'),
    ]:
        a = antes.get(key, 0)
        d = despues.get(key, 0)
        delta = d - a
        sign = '+' if delta > 0 else ''
        print(f"  {label:<28} {a:>10} {d:>10} {sign}{delta:>9}")

    print(f"\n  norm_status:")
    all_statuses = set(
        list(antes.get('norm_status', {}).keys()) +
        list(despues.get('norm_status', {}).keys())
    )
    for s in sorted(all_statuses):
        a = antes.get('norm_status', {}).get(s, 0)
        d = despues.get('norm_status', {}).get(s, 0)
        delta = d - a
        sign = '+' if delta > 0 else ''
        print(f"  {s:<28} {a:>10} {d:>10} {sign}{delta:>9}")

    print(f"{'═' * 65}")


# ═══════════════════════════════════════════════════════════════
# 📋 PASO 1: AGREGAR COLUMNAS NUEVAS
#    ⚠️ Se ejecuta SIEMPRE (incluso en dry-run) porque los
#    pasos siguientes necesitan que las columnas existan
#    para poder hacer SELECT sobre ellas.
# ═══════════════════════════════════════════════════════════════

def step1_add_columns(conn):
    logger.info(
        "\n📋 PASO 1: Verificar/agregar columnas nuevas "
        "(siempre se ejecuta)"
    )
    logger.info("─" * 50)

    existing = get_existing_columns(conn)

    for col, col_type in NEW_COLUMNS.items():
        if col in existing:
            logger.info(f"   ✓ {col} ya existe")
            STATS.columns_existed += 1
        else:
            try:
                conn.execute(
                    f"ALTER TABLE vehicles "
                    f"ADD COLUMN {col} {col_type}"
                )
                logger.info(f"   + {col} ({col_type})")
                STATS.columns_added += 1
            except sqlite3.OperationalError as e:
                logger.warning(f"   ⚠️ {col}: {e}")

    conn.commit()

    logger.info(
        f"   Resultado: {STATS.columns_added} agregadas, "
        f"{STATS.columns_existed} existían"
    )


# ═══════════════════════════════════════════════════════════════
# 📋 PASO 2: POBLAR version_raw
# ═══════════════════════════════════════════════════════════════

def step2_populate_version_raw(conn, dry_run=False):
    logger.info("\n📋 PASO 2: Poblar version_raw desde version")
    logger.info("─" * 50)

    # Contar cuántos ya tienen version_raw
    already = conn.execute(
        "SELECT COUNT(*) FROM vehicles "
        "WHERE version_raw IS NOT NULL AND version_raw != ''"
    ).fetchone()[0]
    STATS.version_raw_already = already

    # Contar pendientes
    pending = conn.execute(
        "SELECT COUNT(*) FROM vehicles "
        "WHERE (version_raw IS NULL OR version_raw = '') "
        "AND version IS NOT NULL AND version != ''"
    ).fetchone()[0]

    logger.info(f"   Ya tienen version_raw: {already}")
    logger.info(f"   Pendientes: {pending}")

    if pending == 0:
        logger.info("   Nada que hacer")
        return

    if not dry_run:
        result = conn.execute("""
            UPDATE vehicles
            SET version_raw = version
            WHERE (version_raw IS NULL OR version_raw = '')
              AND version IS NOT NULL
              AND version != ''
        """)
        STATS.version_raw_populated = result.rowcount
        STATS.total_changes += result.rowcount
        conn.commit()
        logger.info(
            f"   ✅ Poblados: {STATS.version_raw_populated}"
        )
    else:
        STATS.version_raw_populated = pending
        logger.info(f"   [DRY] Poblaría: {pending}")


# ═══════════════════════════════════════════════════════════════
# 📋 PASO 3: EXTRAER METADATA DE TEXTOS EXISTENTES
# ═══════════════════════════════════════════════════════════════

def step3_extract_metadata(conn, dry_run=False):
    logger.info(
        "\n📋 PASO 3: Extraer metadata de textos existentes"
    )
    logger.info("─" * 50)

    # Buscar registros que tengan texto pero no metadata completa
    rows = conn.execute("""
        SELECT id, version, version_raw, descripcion,
               kilometros, puertas, traccion,
               tiene_gnc, es_0km
        FROM vehicles
        WHERE (version IS NOT NULL OR version_raw IS NOT NULL
               OR descripcion IS NOT NULL)
    """).fetchall()

    logger.info(f"   Registros a revisar: {len(rows)}")
    STATS.metadata_processed = len(rows)

    updated = 0
    batch_count = 0

    for row in rows:
        # row es una tupla por defecto
        vid = row[0]
        version_val = row[1] or ''
        version_raw_val = row[2] or ''
        descripcion_val = row[3] or ''
        km = row[4] or 0
        existing_puertas = row[5]
        existing_traccion = row[6]
        existing_gnc = row[7] or 0
        existing_0km = row[8] or 0

        # Combinar todos los textos disponibles
        text = ' '.join(filter(None, [
            version_raw_val, version_val, descripcion_val
        ]))

        if not text.strip():
            continue

        updates = {}

        # Puertas (solo si no tiene)
        if not existing_puertas:
            m = RE_PUERTAS.search(text)
            if m:
                updates['puertas'] = int(m.group(1))
                STATS.puertas_extracted += 1

        # Tracción (solo si no tiene)
        if not existing_traccion:
            m = RE_TRACCION.search(text)
            if m:
                updates['traccion'] = (
                    m.group(1) or m.group(2)
                ).lower()
                STATS.traccion_extracted += 1

        # GNC (solo si no tiene)
        if not existing_gnc:
            if RE_GNC.search(text):
                updates['tiene_gnc'] = 1
                STATS.gnc_detected += 1

        # 0km (solo si no tiene y km==0)
        if not existing_0km:
            if km == 0 and RE_0KM.search(text):
                updates['es_0km'] = 1
                STATS.es_0km_detected += 1

        if updates:
            if not dry_run:
                sets = ', '.join(f"{k} = ?" for k in updates)
                vals = list(updates.values()) + [vid]
                conn.execute(
                    f"UPDATE vehicles SET {sets} WHERE id = ?",
                    vals
                )
            updated += 1
            batch_count += 1

            # Commit en batches
            if not dry_run and batch_count >= 500:
                conn.commit()
                batch_count = 0

    if not dry_run:
        conn.commit()

    STATS.metadata_updated = updated
    STATS.total_changes += updated

    prefix = "[DRY] " if dry_run else "✅ "
    logger.info(f"   {prefix}Registros actualizados: {updated}")
    logger.info(f"      Puertas: {STATS.puertas_extracted}")
    logger.info(f"      Tracción: {STATS.traccion_extracted}")
    logger.info(f"      GNC: {STATS.gnc_detected}")
    logger.info(f"      0km: {STATS.es_0km_detected}")


# ═══════════════════════════════════════════════════════════════
# 📋 PASO 4: RE-NORMALIZAR REGISTROS
# ═══════════════════════════════════════════════════════════════

def step4_renormalize(conn, dry_run=False, dicts_path=None):
    logger.info(
        "\n📋 PASO 4: Re-normalizar registros parciales"
    )
    logger.info("─" * 50)

    if not HAS_NV2:
        logger.warning(
            "   ⚠️ normalizer_v2 no disponible, saltando"
        )
        return

    # Inicializar normalizer
    dicts = dicts_path or DICTS_PATH
    aliases = BRAND_ALIASES_PATH

    if not os.path.exists(dicts):
        logger.warning(f"   ⚠️ {dicts} no encontrado, saltando")
        return

    loaded = nv2.init_normalizer(
        dicts_path=dicts,
        brand_aliases_path=(
            aliases if os.path.exists(aliases) else None
        )
    )
    if not loaded:
        logger.warning("   ⚠️ Catálogo no cargado, saltando")
        return

    logger.info("   ✅ Normalizer inicializado")

    # Obtener candidatos
    placeholders = ','.join('?' * len(RENORM_STATUSES))
    rows = conn.execute(f"""
        SELECT id, marca, modelo, version, version_raw,
               descripcion, año, kilometros, precio_usd,
               transmision, combustible, norm_status,
               norm_confidence, puertas, traccion,
               tiene_gnc, es_0km
        FROM vehicles
        WHERE norm_status IN ({placeholders})
          AND marca IS NOT NULL
          AND marca != ''
        ORDER BY
            CASE norm_status
                WHEN 'partial_match' THEN 1
                WHEN 'fallback' THEN 2
                WHEN 'pending' THEN 3
                ELSE 4
            END,
            id
    """, RENORM_STATUSES).fetchall()

    STATS.renorm_candidates = len(rows)
    logger.info(f"   Candidatos: {len(rows)}")

    if not rows:
        logger.info("   Nada que re-normalizar")
        return

    # Breakdown por status
    status_counts = {}
    for r in rows:
        s = r[11] or 'NULL'
        status_counts[s] = status_counts.get(s, 0) + 1
    for s, c in sorted(
        status_counts.items(), key=lambda x: -x[1]
    ):
        logger.info(f"      {s}: {c}")

    examples_version = []
    examples_model = []
    examples_status = []
    examples_skipped_model = []
    examples_skipped_version = []
    batch_count = 0

    for i, row in enumerate(rows):
        vid = row[0]
        marca = row[1] or ''
        modelo = row[2] or ''
        version = row[3] or ''
        version_raw_val = row[4] or ''
        descripcion = row[5] or ''
        año = row[6]
        kilometros = row[7]
        precio_usd = row[8]
        old_status = row[11] or 'pending'
        old_confidence = row[12] or 0
        existing_puertas = row[13]
        existing_traccion = row[14]
        existing_gnc = row[15] or 0
        existing_0km = row[16] or 0

        try:
            # Preparar input para normalizer
            version_input = version_raw_val or version

            # Pre-limpiar: quitar marca de version
            version_for_norm = version_input
            if marca and version_for_norm:
                version_for_norm = re.sub(
                    r'\b' + re.escape(marca) + r'\b',
                    '', version_for_norm,
                    flags=re.IGNORECASE
                ).strip()

            norm = nv2.normalize_vehicle(
                titulo='',
                descripcion=descripcion,
                marca_raw=marca,
                modelo_raw=modelo,
                version_raw=version_for_norm,
                año_raw=año,
                km_raw=kilometros,
                precio_raw=precio_usd,
            )

            updates = {}
            new_status = norm.get('norm_status', 'fallback')
            new_confidence = norm.get('confidence', 0)

            # ══════════════════════════════════════════
            # GUARDA 1: No "mejorar" modelo si el original
            # es más específico (contiene al nuevo)
            # Ejemplo: "c3 aircross" → "c3" es INCORRECTO
            #          "hrv" → "hr-v" es CORRECTO (alias)
            # ══════════════════════════════════════════
            new_modelo = norm.get('modelo')
            model_changed = False
            if (new_modelo and norm.get('from_catalog')
                    and new_modelo != modelo):
                modelo_lower = modelo.lower().strip()
                new_modelo_lower = new_modelo.lower().strip()

                # Rechazar si el modelo original CONTIENE
                # al nuevo (más específico → más genérico)
                is_subset = (
                    new_modelo_lower in modelo_lower
                    and new_modelo_lower != modelo_lower
                    and len(modelo_lower) > len(new_modelo_lower) + 1
                )

                if is_subset:
                    # El original es más específico, no cambiar
                    if len(examples_skipped_model) < 10:
                        examples_skipped_model.append({
                            'id': vid,
                            'original': modelo,
                            'proposed': new_modelo,
                            'reason': 'original más específico',
                        })
                else:
                    updates['modelo'] = new_modelo
                    model_changed = True
                    STATS.renorm_model_improved += 1
                    if len(examples_model) < 10:
                        examples_model.append({
                            'id': vid,
                            'old': modelo,
                            'new': new_modelo,
                        })

            # ══════════════════════════════════════════
            # GUARDA 2: No asignar versión si no había
            # input de versión (evitar falsos positivos)
            # ══════════════════════════════════════════
            new_version = norm.get('version')
            if new_version and not version:
                # Solo aceptar si había version_input real
                has_real_input = (
                    version_input
                    and len(version_input.strip()) >= 3
                    and version_input.strip().lower() != modelo.lower().strip()
                )

                if has_real_input:
                    updates['version'] = new_version
                    STATS.renorm_version_found += 1
                    if len(examples_version) < 15:
                        examples_version.append({
                            'id': vid,
                            'marca': marca,
                            'modelo': (
                                new_modelo if model_changed
                                else modelo
                            ),
                            'input': version_input[:50],
                            'version': new_version,
                            'method': norm.get(
                                'version_method'
                            ),
                            'conf': new_confidence,
                        })
                else:
                    if len(examples_skipped_version) < 10:
                        examples_skipped_version.append({
                            'id': vid,
                            'marca': marca,
                            'modelo': modelo,
                            'input': repr(version_input[:30]),
                            'proposed': new_version,
                            'reason': 'sin input real',
                        })

            # ── Actualizar status si mejoró ──
            old_rank = STATUS_RANK.get(old_status, 0)
            new_rank = STATUS_RANK.get(new_status, 0)
            if new_rank > old_rank:
                # Si se propuso versión pero se rechazó,
                # no subir a full_match
                if (new_status == 'full_match'
                        and 'version' not in updates
                        and not version):
                    # Máximo partial_match
                    if STATUS_RANK.get(
                        'partial_match', 0
                    ) > old_rank:
                        updates['norm_status'] = 'partial_match'
                        STATS.renorm_status_upgraded += 1
                        if len(examples_status) < 10:
                            examples_status.append({
                                'id': vid,
                                'old': old_status,
                                'new': 'partial_match',
                                'note': 'capped (no version)',
                            })
                else:
                    updates['norm_status'] = new_status
                    STATS.renorm_status_upgraded += 1
                    if len(examples_status) < 10:
                        examples_status.append({
                            'id': vid,
                            'old': old_status,
                            'new': new_status,
                        })

            # ── Confidence ──
            updates['norm_confidence'] = new_confidence

            # ── Marca mejorada ──
            new_marca = norm.get('marca')
            if (new_marca and norm.get('from_catalog')
                    and new_marca != marca):
                updates['marca'] = new_marca

            # ── Metadata del normalizer ──
            extracted = norm.get('extracted_data', {})
            meta_added = False

            if (extracted.get('puertas')
                    and not existing_puertas):
                updates['puertas'] = extracted['puertas']
                meta_added = True

            if (extracted.get('traccion')
                    and not existing_traccion):
                updates['traccion'] = extracted['traccion']
                meta_added = True

            if (extracted.get('tiene_gnc')
                    and not existing_gnc):
                updates['tiene_gnc'] = 1
                meta_added = True

            if (extracted.get('es_0km')
                    and not existing_0km):
                updates['es_0km'] = 1
                meta_added = True

            if meta_added:
                STATS.renorm_metadata_added += 1

            # ── Aplicar ──
            if updates and not dry_run:
                sets = ', '.join(f"{k} = ?" for k in updates)
                vals = list(updates.values()) + [vid]
                conn.execute(
                    f"UPDATE vehicles SET {sets} WHERE id = ?",
                    vals
                )
                STATS.total_changes += 1

                batch_count += 1
                if batch_count >= 500:
                    conn.commit()
                    batch_count = 0
            elif updates and dry_run:
                STATS.total_changes += 1

        except Exception as e:
            STATS.renorm_errors += 1
            logger.debug(
                f"   Error re-normalizando {vid}: {e}"
            )

        # Progress log
        if (i + 1) % 500 == 0 or (i + 1) == len(rows):
            logger.info(
                f"   {i + 1}/{len(rows)} | "
                f"Ver: {STATS.renorm_version_found} | "
                f"Mod: {STATS.renorm_model_improved} | "
                f"Up: {STATS.renorm_status_upgraded}"
            )

    if not dry_run:
        conn.commit()

    # Ejemplos
    prefix = "[DRY] " if dry_run else ""

    if examples_version:
        logger.info(
            f"\n   📋 {prefix}Ejemplos - "
            f"Versiones encontradas:"
        )
        for ex in examples_version[:10]:
            logger.info(
                f"      [{ex['id']}] {ex['marca']} "
                f"{ex['modelo']}: '{ex['input']}' → "
                f"'{ex['version']}' ({ex['method']}, "
                f"conf:{ex['conf']})"
            )

    if examples_skipped_version:
        logger.info(
            f"\n   🚫 {prefix}Versiones RECHAZADAS "
            f"(sin input real):"
        )
        for ex in examples_skipped_version[:5]:
            logger.info(
                f"      [{ex['id']}] {ex['marca']} "
                f"{ex['modelo']}: input={ex['input']} → "
                f"propuso '{ex['proposed']}' — {ex['reason']}"
            )

    if examples_model:
        logger.info(
            f"\n   📋 {prefix}Ejemplos - "
            f"Modelos mejorados:"
        )
        for ex in examples_model[:5]:
            logger.info(
                f"      [{ex['id']}] '{ex['old']}' → "
                f"'{ex['new']}'"
            )

    if examples_skipped_model:
        logger.info(
            f"\n   🚫 {prefix}Modelos RECHAZADOS "
            f"(original más específico):"
        )
        for ex in examples_skipped_model[:5]:
            logger.info(
                f"      [{ex['id']}] '{ex['original']}' → "
                f"'{ex['proposed']}' — {ex['reason']}"
            )

    if examples_status:
        logger.info(
            f"\n   📋 {prefix}Ejemplos - Status upgrades:"
        )
        for ex in examples_status[:5]:
            note = f" ({ex['note']})" if 'note' in ex else ''
            logger.info(
                f"      [{ex['id']}] {ex['old']} → "
                f"{ex['new']}{note}"
            )

    logger.info(
        f"\n   {'✅' if not dry_run else '⚡ [DRY]'} "
        f"Re-normalización completada: "
        f"{STATS.renorm_version_found} versiones, "
        f"{STATS.renorm_model_improved} modelos, "
        f"{STATS.renorm_status_upgraded} status upgrades"
    )


# ═══════════════════════════════════════════════════════════════
# 🚀 MAIN
# ═══════════════════════════════════════════════════════════════

def run_migration(db_path, dry_run=False, dicts_path=None):
    global STATS
    STATS = MigrationStats()

    logger.info("=" * 60)
    logger.info("🔄 MIGRACIÓN RETROACTIVA DE DATOS v1.1")
    logger.info("=" * 60)

    if dry_run:
        logger.info(
            "⚡ MODO DRY-RUN: solo las columnas se agregan "
            "(necesarias para consultas), los datos NO se "
            "modifican"
        )

    if not os.path.exists(db_path):
        logger.error(f"❌ BD no encontrada: {db_path}")
        sys.exit(1)

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    logger.info(f"📂 BD: {db_path} ({size_mb:.1f} MB)")

    # Backup
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = f"{db_path}.pre_migration_{timestamp}"
    if not dry_run:
        shutil.copy2(db_path, backup)
        logger.info(f"💾 Backup: {backup}")
    else:
        logger.info(
            f"💾 [DRY] Backup se haría en: {backup}"
        )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Estadísticas ANTES
    logger.info("\n📊 Estado ANTES de migración:")
    stats_antes = get_full_stats(conn)
    STATS.total_vehicles = stats_antes['total']
    logger.info(f"   Total: {stats_antes['total']}")
    logger.info(f"   Activos: {stats_antes['activos']}")
    logger.info(f"   Con marca: {stats_antes['con_marca']}")
    logger.info(f"   Con modelo: {stats_antes['con_modelo']}")
    logger.info(
        f"   Con versión: {stats_antes['con_version']}"
    )
    logger.info("   norm_status:")
    for s, c in stats_antes['norm_status'].items():
        logger.info(f"      {s}: {c}")

    # ── PASO 1: SIEMPRE se ejecuta (schema) ──
    step1_add_columns(conn)

    # ── PASOS 2-4: respetan dry-run ──
    step2_populate_version_raw(conn, dry_run)
    step3_extract_metadata(conn, dry_run)
    step4_renormalize(conn, dry_run, dicts_path)

    # Estadísticas DESPUÉS
    if not dry_run:
        conn.commit()

    stats_despues = get_full_stats(conn)

    # Comparación
    print_comparison(stats_antes, stats_despues)

    # Guardar resumen para GitHub Actions
    summary = {
        'antes': {
            k: v for k, v in stats_antes.items()
            if k != 'norm_status'
        },
        'despues': {
            k: v for k, v in stats_despues.items()
            if k != 'norm_status'
        },
        'antes_norm': stats_antes.get('norm_status', {}),
        'despues_norm': stats_despues.get('norm_status', {}),
        'migration_stats': {
            'columns_added': STATS.columns_added,
            'version_raw_populated':
                STATS.version_raw_populated,
            'metadata_updated': STATS.metadata_updated,
            'puertas': STATS.puertas_extracted,
            'traccion': STATS.traccion_extracted,
            'gnc': STATS.gnc_detected,
            'es_0km': STATS.es_0km_detected,
            'versions_found': STATS.renorm_version_found,
            'models_improved': STATS.renorm_model_improved,
            'status_upgraded': STATS.renorm_status_upgraded,
            'errors': STATS.renorm_errors,
            'total_changes': STATS.total_changes,
        },
        'dry_run': dry_run,
    }
    with open('migration_summary.json', 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info("📄 Resumen guardado: migration_summary.json")

    conn.close()

    # Resumen final
    logger.info(f"\n{STATS.summary()}")

    if dry_run:
        logger.info(
            "\n⚡ DRY-RUN: solo columnas fueron agregadas, "
            "datos NO modificados"
        )

    logger.info("\n✅ MIGRACIÓN COMPLETADA")
    logger.info("=" * 60)

    return STATS


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            '🔄 Migración retroactiva de datos '
            'existentes v1.1'
        )
    )
    parser.add_argument(
        '--db', default=DEFAULT_DB,
        help=f'Ruta a la BD (default: {DEFAULT_DB})'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help=(
            'Simular sin modificar datos '
            '(columnas sí se agregan)'
        )
    )
    parser.add_argument(
        '--dicts-path', default=DICTS_PATH,
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

    stats = run_migration(
        db_path=args.db,
        dry_run=args.dry_run,
        dicts_path=args.dicts_path,
    )

    if stats.renorm_errors > max(
        stats.renorm_candidates * 0.2, 50
    ):
        logger.error(
            "❌ Demasiados errores en re-normalización"
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
