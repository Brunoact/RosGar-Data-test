"""
🔄 migrate_old_data.py - Migración retroactiva de datos existentes
===================================================================
Aplica las mejoras nuevas a los registros viejos:
1. Agrega columnas nuevas al schema
2. Extrae metadata (puertas, tracción, GNC, 0km) de version_raw/descripcion
3. Re-normaliza registros con partial_match/fallback
4. Reclasifica norm_status

USO:
  python migrate_old_data.py                    # Ejecutar
  python migrate_old_data.py --dry-run          # Simular
  python migrate_old_data.py --db otra.db       # BD alternativa
"""

import sqlite3
import sys
import os
import re
import logging
import shutil
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

try:
    import normalizer_v2 as nv2
    HAS_NV2 = True
except ImportError:
    HAS_NV2 = False

DB_PATH = 'rosariogarage.db'
DRY_RUN = '--dry-run' in sys.argv
if '--db' in sys.argv:
    idx = sys.argv.index('--db')
    DB_PATH = sys.argv[idx + 1]

RE_PUERTAS = re.compile(r'\b([345])\s*[pP](?:uertas?)?\b')
RE_TRACCION = re.compile(r'\b(4x[24])\b|\b(AWD|FWD|RWD)\b', re.IGNORECASE)
RE_GNC = re.compile(
    r'\bGNC\b|\bgas\s*natural\b|\bc[/\s]?GNC\b|\bcon\s+GNC\b',
    re.IGNORECASE
)
RE_0KM = re.compile(r'\b0\s*km\b|\bcero\s*km\b|\bokm\b', re.IGNORECASE)


def main():
    logger.info("=" * 60)
    logger.info("🔄 MIGRACIÓN RETROACTIVA DE DATOS")
    logger.info("=" * 60)

    if not os.path.exists(DB_PATH):
        logger.error(f"❌ BD no encontrada: {DB_PATH}")
        sys.exit(1)

    # Backup
    backup = f"{DB_PATH}.pre_migration_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(DB_PATH, backup)
    logger.info(f"💾 Backup: {backup}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # ── PASO 1: Agregar columnas nuevas ──
    logger.info("\n📋 Paso 1: Verificar/agregar columnas")
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

    for col, col_type in new_columns.items():
        if col not in existing:
            if not DRY_RUN:
                conn.execute(
                    f"ALTER TABLE vehicles ADD COLUMN {col} {col_type}"
                )
            logger.info(f"   + {col} ({col_type})")
        else:
            logger.info(f"   ✓ {col} ya existe")

    if not DRY_RUN:
        conn.commit()

    # ── PASO 2: Poblar version_raw desde version donde no exista ──
    logger.info("\n📋 Paso 2: Poblar version_raw desde version existente")
    if not DRY_RUN:
        result = conn.execute("""
            UPDATE vehicles
            SET version_raw = version
            WHERE version_raw IS NULL
              AND version IS NOT NULL
              AND version != ''
        """)
        logger.info(f"   Actualizados: {result.rowcount}")
        conn.commit()
    else:
        count = conn.execute("""
            SELECT COUNT(*) FROM vehicles
            WHERE version_raw IS NULL
              AND version IS NOT NULL AND version != ''
        """).fetchone()[0]
        logger.info(f"   Pendientes: {count}")

    # ── PASO 3: Extraer metadata de textos existentes ──
    logger.info("\n📋 Paso 3: Extraer metadata de version/version_raw")

    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, version, version_raw, descripcion, kilometros
        FROM vehicles
        WHERE puertas IS NULL
          AND traccion IS NULL
          AND tiene_gnc = 0
          AND es_0km = 0
    """).fetchall()

    logger.info(f"   Registros a procesar: {len(rows)}")
    stats = {'puertas': 0, 'traccion': 0, 'gnc': 0, '0km': 0}

    for row in rows:
        row = dict(row)
        text = ' '.join(filter(None, [
            row.get('version_raw', ''),
            row.get('version', ''),
            row.get('descripcion', ''),
        ]))
        if not text.strip():
            continue

        updates = {}

        m = RE_PUERTAS.search(text)
        if m:
            updates['puertas'] = int(m.group(1))
            stats['puertas'] += 1

        m = RE_TRACCION.search(text)
        if m:
            updates['traccion'] = (m.group(1) or m.group(2)).lower()
            stats['traccion'] += 1

        if RE_GNC.search(text):
            updates['tiene_gnc'] = 1
            stats['gnc'] += 1

        km = row.get('kilometros', 0)
        if km == 0 and RE_0KM.search(text):
            updates['es_0km'] = 1
            stats['0km'] += 1

        if updates and not DRY_RUN:
            sets = ', '.join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [row['id']]
            conn.execute(
                f"UPDATE vehicles SET {sets} WHERE id = ?", vals
            )

    if not DRY_RUN:
        conn.commit()

    logger.info(f"   Puertas: {stats['puertas']}")
    logger.info(f"   Tracción: {stats['traccion']}")
    logger.info(f"   GNC: {stats['gnc']}")
    logger.info(f"   0km: {stats['0km']}")

    # ── PASO 4: Re-normalizar con normalizer v2 ──
    logger.info("\n📋 Paso 4: Re-normalizar registros sin versión")

    if not HAS_NV2:
        logger.warning("⚠️ normalizer_v2 no disponible, saltando")
    else:
        loaded = nv2.init_normalizer(
            dicts_path="config/normalizer_dicts.json"
        )
        if not loaded:
            logger.warning("⚠️ Catálogo no cargado, saltando")
        else:
            # Re-normalizar: partial_match, fallback, pending
            rows = conn.execute("""
                SELECT id, marca, modelo, version, version_raw,
                       descripcion, año, kilometros, precio_usd
                FROM vehicles
                WHERE norm_status IN (
                    'partial_match', 'fallback', 'pending',
                    'unknown', 'no_modelo'
                )
                  AND marca IS NOT NULL
            """).fetchall()

            logger.info(f"   Candidatos: {len(rows)}")
            upgraded = 0
            improved = 0

            for row in rows:
                row = dict(row)
                try:
                    norm = nv2.normalize_vehicle(
                        titulo='',
                        descripcion=row.get('descripcion', ''),
                        marca_raw=row.get('marca', ''),
                        modelo_raw=row.get('modelo', ''),
                        version_raw=row.get('version_raw', '')
                                   or row.get('version', ''),
                        año_raw=row.get('año'),
                        km_raw=row.get('kilometros'),
                        precio_raw=row.get('precio_usd'),
                    )

                    updates = {}

                    # Actualizar modelo si mejoró
                    if (norm.get('modelo') and norm.get('from_catalog')
                            and norm['modelo'] != row.get('modelo')):
                        updates['modelo'] = norm['modelo']

                    # Actualizar versión si encontró
                    if norm.get('version') and not row.get('version'):
                        updates['version'] = norm['version']
                        upgraded += 1

                    # Actualizar status
                    new_status = norm.get('norm_status', 'fallback')
                    old_status = row.get('norm_status', 'pending')

                    status_rank = {
                        'full_match': 5,
                        'partial_match': 4,
                        'marca_only': 3,
                        'fallback': 2,
                        'pending': 1,
                        'unknown': 0,
                        'no_modelo': 0,
                    }
                    if status_rank.get(new_status, 0) > status_rank.get(
                        old_status, 0
                    ):
                        updates['norm_status'] = new_status
                        improved += 1

                    updates['norm_confidence'] = norm.get(
                        'confidence', 0
                    )

                    # Metadata
                    ext = norm.get('extracted_data', {})
                    if ext.get('puertas'):
                        updates['puertas'] = ext['puertas']
                    if ext.get('traccion'):
                        updates['traccion'] = ext['traccion']
                    if ext.get('tiene_gnc'):
                        updates['tiene_gnc'] = 1
                    if ext.get('es_0km'):
                        updates['es_0km'] = 1

                    if updates and not DRY_RUN:
                        sets = ', '.join(
                            f"{k} = ?" for k in updates
                        )
                        vals = list(updates.values()) + [row['id']]
                        conn.execute(
                            f"UPDATE vehicles SET {sets} WHERE id = ?",
                            vals
                        )

                except Exception as e:
                    logger.debug(f"Error re-normalizando {row['id']}: {e}")

            if not DRY_RUN:
                conn.commit()

            logger.info(f"   Versiones encontradas: {upgraded}")
            logger.info(f"   Status mejorados: {improved}")

    # ── PASO 5: Estadísticas finales ──
    logger.info("\n" + "=" * 60)
    logger.info("📊 ESTADO FINAL")
    logger.info("=" * 60)

    total = conn.execute(
        "SELECT COUNT(*) FROM vehicles"
    ).fetchone()[0]
    logger.info(f"   Total: {total}")

    rows = conn.execute("""
        SELECT norm_status, COUNT(*) as c
        FROM vehicles
        GROUP BY norm_status
        ORDER BY c DESC
    """).fetchall()
    for row in rows:
        logger.info(f"   {row['norm_status'] or 'NULL'}: {row['c']}")

    meta_stats = conn.execute("""
        SELECT
            SUM(CASE WHEN puertas IS NOT NULL THEN 1 ELSE 0 END) as puertas,
            SUM(CASE WHEN traccion IS NOT NULL THEN 1 ELSE 0 END) as traccion,
            SUM(tiene_gnc) as gnc,
            SUM(es_0km) as es_0km,
            SUM(CASE WHEN version_raw IS NOT NULL THEN 1 ELSE 0 END) as version_raw,
            SUM(CASE WHEN descripcion IS NOT NULL THEN 1 ELSE 0 END) as descripcion
        FROM vehicles
    """).fetchone()

    logger.info(f"\n   Metadata poblada:")
    logger.info(f"      Puertas: {meta_stats[0]}")
    logger.info(f"      Tracción: {meta_stats[1]}")
    logger.info(f"      GNC: {meta_stats[2]}")
    logger.info(f"      0km: {meta_stats[3]}")
    logger.info(f"      version_raw: {meta_stats[4]}")
    logger.info(f"      descripcion: {meta_stats[5]}")

    conn.close()

    if DRY_RUN:
        logger.info("\n⚡ DRY-RUN: ningún cambio aplicado")

    logger.info("\n✅ MIGRACIÓN COMPLETADA")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
