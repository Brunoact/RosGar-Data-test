#!/usr/bin/env python3
"""
🔍 diagnose_versions.py - Diagnóstico de versiones por marca
=============================================================
Detecta por qué una marca específica (ej: Chevrolet) no tiene
versiones en la BD mientras otras sí.

Analiza:
  1. normalizer_dicts.json (catálogo)
  2. rosariogarage.db (datos reales)
  3. Cruce entre ambos

USO:
  python diagnose_versions.py
  python diagnose_versions.py --marca chevrolet
  python diagnose_versions.py --db otra.db --fix
"""

import json
import sqlite3
import os
import sys
import re
import argparse
import logging
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════
# ⚙️ CONFIG
# ═══════════════════════════════════════════════════════════════

DEFAULT_DB = 'rosariogarage.db'
DEFAULT_DICTS = 'config/normalizer_dicts.json'
DEFAULT_ALIASES = 'config/brand_aliases.json'

logger = logging.getLogger(__name__)


def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s │ %(levelname)-7s │ %(message)s',
        datefmt='%H:%M:%S'
    )


def normalize_key(text: str) -> str:
    if not text:
        return ""
    text = str(text).lower().strip()
    replacements = {
        'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
        'ñ': 'n', 'ü': 'u',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r'[^\w\s\-]', ' ', text)
    return ' '.join(text.split())


# ═══════════════════════════════════════════════════════════════
# 📚 ANÁLISIS DEL CATÁLOGO (normalizer_dicts.json)
# ═══════════════════════════════════════════════════════════════

def analyze_catalog(dicts_path: str, target_marca: str = None) -> Dict:
    """Analiza el archivo normalizer_dicts.json."""
    results = {
        'loaded': False,
        'brands': {},
        'issues': [],
        'target_analysis': None,
    }

    if not os.path.exists(dicts_path):
        results['issues'].append(f"❌ Archivo no encontrado: {dicts_path}")
        return results

    try:
        with open(dicts_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        results['loaded'] = True
    except Exception as e:
        results['issues'].append(f"❌ Error leyendo JSON: {e}")
        return results

    catalog_index = data.get('catalog_index', {})
    valid_versions = data.get('valid_versions', {})
    year_ranges = data.get('year_ranges', {})
    code_mappings = data.get('model_code_to_catalog', {})
    model_aliases = data.get('model_aliases', {})
    metadata = data.get('_metadata', {})

    logger.info(f"📚 Metadata: {json.dumps(metadata, indent=2)}")

    # ── Análisis por marca ──
    for marca, modelos in catalog_index.items():
        marca_info = {
            'modelos_catalog': len(modelos),
            'modelos_con_versiones': 0,
            'total_versiones': 0,
            'modelos_sin_versiones': [],
            'modelos_con_versiones_list': [],
            'key_issues': [],
        }

        for modelo in modelos:
            key = f"{marca}|{modelo}"
            vers = valid_versions.get(key, [])

            if vers:
                marca_info['modelos_con_versiones'] += 1
                marca_info['total_versiones'] += len(vers)
                marca_info['modelos_con_versiones_list'].append(
                    (modelo, len(vers))
                )
            else:
                marca_info['modelos_sin_versiones'].append(modelo)

                # Buscar keys similares que podrían ser el problema
                similar = [
                    k for k in valid_versions.keys()
                    if modelo in k and marca in k
                ]
                if similar:
                    marca_info['key_issues'].append({
                        'modelo': modelo,
                        'expected_key': key,
                        'found_similar': similar,
                    })

        results['brands'][marca] = marca_info

    # ── Verificar problemas de whitespace/encoding ──
    for key in valid_versions.keys():
        if key != key.strip():
            results['issues'].append(
                f"⚠️ Whitespace en key: repr={repr(key)}"
            )
        if '|' not in key:
            results['issues'].append(
                f"⚠️ Key sin separador '|': '{key}'"
            )
        parts = key.split('|')
        if len(parts) == 2:
            if (parts[0] != parts[0].strip() or
                    parts[1] != parts[1].strip()):
                results['issues'].append(
                    f"⚠️ Whitespace en partes de key: "
                    f"'{repr(parts[0])}' | '{repr(parts[1])}'"
                )

    # ── Verificar que keys de valid_versions matchean catalog_index ──
    orphan_version_keys = []
    for key in valid_versions.keys():
        if key.startswith('_'):
            continue
        parts = key.split('|')
        if len(parts) != 2:
            continue
        marca_k, modelo_k = parts
        if marca_k not in catalog_index:
            orphan_version_keys.append(
                f"Marca '{marca_k}' no existe en catalog_index"
            )
        elif modelo_k not in catalog_index.get(marca_k, {}):
            orphan_version_keys.append(
                f"Modelo '{modelo_k}' no existe en "
                f"catalog_index['{marca_k}']"
            )

    if orphan_version_keys:
        results['issues'].append(
            f"⚠️ {len(orphan_version_keys)} keys en valid_versions "
            f"sin correspondencia en catalog_index"
        )
        for o in orphan_version_keys[:10]:
            results['issues'].append(f"   → {o}")

    # ── Análisis específico de marca target ──
    if target_marca:
        target = normalize_key(target_marca)
        results['target_analysis'] = analyze_target_marca(
            target, data, catalog_index, valid_versions,
            code_mappings, model_aliases
        )

    return results


def analyze_target_marca(
    target: str,
    data: Dict,
    catalog_index: Dict,
    valid_versions: Dict,
    code_mappings: Dict,
    model_aliases: Dict,
) -> Dict:
    """Análisis profundo de una marca específica."""
    analysis = {
        'marca': target,
        'in_catalog': target in catalog_index,
        'similar_keys': [],
        'models': {},
        'code_mappings': {},
        'aliases': {},
        'version_keys_found': [],
        'version_keys_missing': [],
        'issues': [],
        'recommendations': [],
    }

    # Buscar marca en catálogo (incluyendo variantes)
    marca_variants = [
        target,
        target.replace(' ', '-'),
        target.replace('-', ' '),
        target.replace(' ', ''),
    ]

    found_marca = None
    for variant in marca_variants:
        if variant in catalog_index:
            found_marca = variant
            break

    if not found_marca:
        # Buscar por substring
        for marca_key in catalog_index.keys():
            if target in marca_key or marca_key in target:
                analysis['similar_keys'].append(marca_key)
        analysis['issues'].append(
            f"Marca '{target}' no encontrada en catalog_index. "
            f"Similares: {analysis['similar_keys']}"
        )
        return analysis

    analysis['in_catalog'] = True
    analysis['found_as'] = found_marca
    modelos = catalog_index[found_marca]

    # Analizar cada modelo
    for modelo, modelo_data in modelos.items():
        key = f"{found_marca}|{modelo}"
        vers = valid_versions.get(key, [])

        model_info = {
            'key': key,
            'has_versions': len(vers) > 0,
            'version_count': len(vers),
            'versions': vers[:10],
            'year_range': data.get('year_ranges', {}).get(key),
        }

        # Verificar si el key existe literalmente
        if key in valid_versions:
            analysis['version_keys_found'].append(key)
        else:
            analysis['version_keys_missing'].append(key)

            # Buscar keys similares
            similar = [
                (k, len(valid_versions[k]))
                for k in valid_versions.keys()
                if modelo in k.lower()
            ]
            if similar:
                model_info['similar_version_keys'] = similar

        analysis['models'][modelo] = model_info

    # Code mappings para esta marca
    analysis['code_mappings'] = code_mappings.get(found_marca, {})

    # Aliases para esta marca
    analysis['aliases'] = model_aliases.get(found_marca, {})

    # Recomendaciones
    missing = analysis['version_keys_missing']
    found = analysis['version_keys_found']

    if len(missing) > 0 and len(found) == 0:
        analysis['recommendations'].append(
            f"🔴 CRÍTICO: La marca '{target}' tiene "
            f"{len(modelos)} modelos en catalog_index pero "
            f"NINGUNO tiene versiones en valid_versions"
        )
    elif len(missing) > len(found):
        analysis['recommendations'].append(
            f"🟡 La mayoría de modelos de '{target}' "
            f"({len(missing)}/{len(modelos)}) no tienen versiones"
        )

    return analysis


# ═══════════════════════════════════════════════════════════════
# 💾 ANÁLISIS DE LA BD
# ═══════════════════════════════════════════════════════════════

def analyze_database(db_path: str, target_marca: str = None) -> Dict:
    """Analiza la BD buscando problemas de versiones."""
    results = {
        'loaded': False,
        'total': 0,
        'by_brand': {},
        'target_analysis': None,
    }

    if not os.path.exists(db_path):
        logger.warning(f"⚠️ BD no encontrada: {db_path}")
        return results

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        results['loaded'] = True
    except Exception as e:
        logger.error(f"❌ Error abriendo BD: {e}")
        return results

    # Total
    results['total'] = conn.execute(
        "SELECT COUNT(*) FROM vehicles WHERE activo = 1"
    ).fetchone()[0]

    # Por marca: contar versiones
    rows = conn.execute("""
        SELECT
            marca,
            COUNT(*) as total,
            SUM(CASE WHEN version IS NOT NULL
                AND version != '' THEN 1 ELSE 0 END) as con_version,
            SUM(CASE WHEN modelo IS NOT NULL
                AND modelo != '' THEN 1 ELSE 0 END) as con_modelo,
            SUM(CASE WHEN norm_status = 'full_match'
                THEN 1 ELSE 0 END) as full_match,
            SUM(CASE WHEN norm_status = 'partial_match'
                THEN 1 ELSE 0 END) as partial_match,
            SUM(CASE WHEN norm_status = 'fallback'
                THEN 1 ELSE 0 END) as fallback
        FROM vehicles
        WHERE activo = 1 AND marca IS NOT NULL
        GROUP BY marca
        ORDER BY total DESC
    """).fetchall()

    for row in rows:
        marca = row['marca']
        results['by_brand'][marca] = {
            'total': row['total'],
            'con_version': row['con_version'],
            'con_modelo': row['con_modelo'],
            'full_match': row['full_match'],
            'partial_match': row['partial_match'],
            'fallback': row['fallback'],
            'pct_version': round(
                row['con_version'] / row['total'] * 100, 1
            ) if row['total'] > 0 else 0,
        }

    # Análisis específico de marca target
    if target_marca:
        target = normalize_key(target_marca)
        target_rows = conn.execute("""
            SELECT id, marca, modelo, version, año,
                   norm_status, url
            FROM vehicles
            WHERE activo = 1
                AND LOWER(marca) = ?
            ORDER BY modelo, año
        """, (target,)).fetchall()

        target_info = {
            'total': len(target_rows),
            'samples_without_version': [],
            'samples_with_version': [],
            'models_breakdown': defaultdict(lambda: {
                'total': 0, 'with_ver': 0, 'without_ver': 0,
                'statuses': Counter()
            }),
        }

        for row in target_rows:
            modelo = row['modelo'] or '(sin modelo)'
            mb = target_info['models_breakdown'][modelo]
            mb['total'] += 1
            mb['statuses'][row['norm_status'] or 'null'] += 1

            if row['version']:
                mb['with_ver'] += 1
                if len(target_info['samples_with_version']) < 5:
                    target_info['samples_with_version'].append({
                        'id': row['id'],
                        'modelo': row['modelo'],
                        'version': row['version'],
                        'año': row['año'],
                        'status': row['norm_status'],
                    })
            else:
                mb['without_ver'] += 1
                if len(target_info['samples_without_version']) < 10:
                    target_info['samples_without_version'].append({
                        'id': row['id'],
                        'modelo': row['modelo'],
                        'version': row['version'],
                        'año': row['año'],
                        'status': row['norm_status'],
                        'url': row['url'],
                    })

        results['target_analysis'] = target_info

    conn.close()
    return results


# ═══════════════════════════════════════════════════════════════
# 🔧 FIX: Regenerar versiones faltantes
# ═══════════════════════════════════════════════════════════════

def fix_missing_versions(
    db_path: str,
    dicts_path: str,
    target_marca: str = None,
    dry_run: bool = True,
) -> Dict:
    """
    Intenta re-normalizar vehículos sin versión usando el catálogo.
    Solo para vehículos que tienen marca+modelo pero no versión.
    """
    fix_results = {
        'analyzed': 0,
        'fixed': 0,
        'already_ok': 0,
        'no_fix_available': 0,
        'errors': 0,
        'examples': [],
    }

    if not os.path.exists(db_path):
        logger.error(f"❌ BD no encontrada: {db_path}")
        return fix_results

    if not os.path.exists(dicts_path):
        logger.error(f"❌ Catálogo no encontrado: {dicts_path}")
        return fix_results

    # Cargar catálogo
    try:
        with open(dicts_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        valid_versions = data.get('valid_versions', {})
    except Exception as e:
        logger.error(f"❌ Error cargando catálogo: {e}")
        return fix_results

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Buscar vehículos sin versión que tienen marca+modelo
    query = """
        SELECT id, url, marca, modelo, version, año,
               norm_status
        FROM vehicles
        WHERE activo = 1
            AND marca IS NOT NULL AND marca != ''
            AND modelo IS NOT NULL AND modelo != ''
            AND (version IS NULL OR version = '')
    """
    params = []

    if target_marca:
        query += " AND LOWER(marca) = ?"
        params.append(normalize_key(target_marca))

    rows = conn.execute(query, params).fetchall()
    logger.info(f"🔍 Vehículos sin versión: {len(rows)}")

    # Intentar importar normalizer para re-procesar
    try:
        from normalizer_v2 import (
            init_normalizer,
            normalize_vehicle,
        )
        init_normalizer(dicts_path=dicts_path)
        has_normalizer = True
        logger.info("✅ Normalizer cargado para re-procesamiento")
    except ImportError:
        has_normalizer = False
        logger.warning(
            "⚠️ normalizer_v2 no disponible, "
            "usando búsqueda directa"
        )

    for row in rows:
        fix_results['analyzed'] += 1
        vid = row['id']
        marca = normalize_key(row['marca'])
        modelo = normalize_key(row['modelo'])
        url = row['url']

        # Buscar versiones en catálogo
        key = f"{marca}|{modelo}"
        versiones = valid_versions.get(key, [])

        if not versiones:
            fix_results['no_fix_available'] += 1
            continue

        # Si hay normalizer, re-procesar la página
        # Si no, intentar match directo de versión en el título/url
        version_found = None

        if has_normalizer:
            # Intentar con los datos que tenemos
            # (sin re-scrapear)
            try:
                result = normalize_vehicle(
                    titulo="",
                    descripcion="",
                    marca_raw=row['marca'],
                    modelo_raw=row['modelo'],
                    version_raw="",
                    año_raw=row['año'],
                )
                if result.get('version'):
                    version_found = result['version']
            except Exception:
                pass

        if not version_found:
            # Búsqueda directa: ver si alguna versión del
            # catálogo aparece en la URL
            url_lower = (url or '').lower()
            for ver in sorted(versiones, key=len, reverse=True):
                ver_norm = normalize_key(ver)
                if ver_norm and len(ver_norm) >= 3:
                    if ver_norm in url_lower:
                        version_found = ver
                        break

        if version_found:
            fix_results['fixed'] += 1
            if len(fix_results['examples']) < 20:
                fix_results['examples'].append({
                    'id': vid,
                    'marca': marca,
                    'modelo': modelo,
                    'version_new': version_found,
                    'old_status': row['norm_status'],
                })

            if not dry_run:
                conn.execute("""
                    UPDATE vehicles
                    SET version = ?,
                        norm_status = 'full_match'
                    WHERE id = ?
                """, (version_found, vid))
        else:
            fix_results['no_fix_available'] += 1

    if not dry_run:
        conn.commit()
        logger.info(
            f"✅ {fix_results['fixed']} vehículos actualizados"
        )
    else:
        logger.info(
            f"⚡ DRY-RUN: {fix_results['fixed']} "
            f"vehículos se actualizarían"
        )

    conn.close()
    return fix_results


# ═══════════════════════════════════════════════════════════════
# 📊 REPORTE
# ═══════════════════════════════════════════════════════════════

def print_full_report(
    catalog_results: Dict,
    db_results: Dict,
    target_marca: str = None,
    fix_results: Dict = None,
):
    """Imprime reporte completo."""
    print("\n" + "=" * 70)
    print("🔍 DIAGNÓSTICO DE VERSIONES")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # ── CATÁLOGO ──
    if catalog_results['loaded']:
        print("\n📚 CATÁLOGO (normalizer_dicts.json)")
        print("-" * 50)

        # Tabla comparativa
        print(f"\n{'Marca':<20} {'Modelos':>8} {'Con Ver':>8} "
              f"{'Sin Ver':>8} {'%':>6}")
        print("-" * 56)

        brands = catalog_results['brands']
        sorted_brands = sorted(
            brands.items(),
            key=lambda x: x[1]['modelos_catalog'],
            reverse=True
        )

        for marca, info in sorted_brands:
            con = info['modelos_con_versiones']
            total = info['modelos_catalog']
            sin = len(info['modelos_sin_versiones'])
            pct = round(con / total * 100, 1) if total > 0 else 0

            flag = ""
            if pct == 0 and total > 0:
                flag = " 🔴"
            elif pct < 50:
                flag = " 🟡"

            print(
                f"  {marca:<18} {total:>8} {con:>8} "
                f"{sin:>8} {pct:>5.1f}%{flag}"
            )

        # Issues
        if catalog_results['issues']:
            print(f"\n⚠️ PROBLEMAS DETECTADOS EN CATÁLOGO:")
            for issue in catalog_results['issues']:
                print(f"  {issue}")

    # ── BD ──
    if db_results['loaded']:
        print(f"\n💾 BASE DE DATOS")
        print("-" * 50)
        print(f"Total activos: {db_results['total']}")

        print(f"\n{'Marca':<20} {'Total':>6} {'Modelo':>7} "
              f"{'Versión':>8} {'%Ver':>6} "
              f"{'Full':>5} {'Partial':>8} {'Fall':>5}")
        print("-" * 75)

        sorted_db = sorted(
            db_results['by_brand'].items(),
            key=lambda x: x[1]['total'],
            reverse=True
        )

        for marca, info in sorted_db[:20]:
            flag = ""
            if info['pct_version'] == 0 and info['total'] > 5:
                flag = " 🔴"
            elif info['pct_version'] < 30:
                flag = " 🟡"

            print(
                f"  {marca:<18} {info['total']:>6} "
                f"{info['con_modelo']:>7} "
                f"{info['con_version']:>8} "
                f"{info['pct_version']:>5.1f}%"
                f"{info['full_match']:>5} "
                f"{info['partial_match']:>8} "
                f"{info['fallback']:>5}{flag}"
            )

    # ── ANÁLISIS TARGET ──
    if target_marca:
        print(f"\n{'=' * 70}")
        print(f"🎯 ANÁLISIS DETALLADO: {target_marca.upper()}")
        print(f"{'=' * 70}")

        # Catálogo target
        ct = catalog_results.get('target_analysis')
        if ct:
            print(f"\n📚 En catálogo:")
            print(f"  Encontrada como: '{ct.get('found_as', '?')}'")

            if ct['version_keys_found']:
                print(f"\n  ✅ Modelos CON versiones "
                      f"({len(ct['version_keys_found'])}):")
                for key in ct['version_keys_found'][:15]:
                    modelo = key.split('|')[1] if '|' in key else key
                    model_info = ct['models'].get(modelo, {})
                    vers = model_info.get('versions', [])
                    print(
                        f"    {modelo:<20} "
                        f"{model_info.get('version_count', 0)} "
                        f"versiones: {vers[:3]}"
                    )

            if ct['version_keys_missing']:
                print(f"\n  ❌ Modelos SIN versiones "
                      f"({len(ct['version_keys_missing'])}):")
                for key in ct['version_keys_missing'][:15]:
                    modelo = key.split('|')[1] if '|' in key else key
                    model_info = ct['models'].get(modelo, {})
                    similar = model_info.get(
                        'similar_version_keys', []
                    )
                    line = f"    {modelo:<20} key='{key}'"
                    if similar:
                        line += f" → SIMILAR: {similar}"
                    print(line)

            if ct.get('code_mappings'):
                print(f"\n  🔤 Code mappings "
                      f"({len(ct['code_mappings'])}):")
                for code, mapped in list(
                    ct['code_mappings'].items()
                )[:10]:
                    print(f"    {code} → {mapped}")

            if ct.get('recommendations'):
                print(f"\n  💡 Recomendaciones:")
                for rec in ct['recommendations']:
                    print(f"    {rec}")

        # BD target
        dt = db_results.get('target_analysis')
        if dt:
            print(f"\n💾 En base de datos:")
            print(f"  Total: {dt['total']}")

            if dt['models_breakdown']:
                print(f"\n  Desglose por modelo:")
                for modelo, info in sorted(
                    dt['models_breakdown'].items(),
                    key=lambda x: x[1]['total'],
                    reverse=True,
                )[:15]:
                    status_str = ', '.join(
                        f"{k}:{v}"
                        for k, v in info['statuses'].items()
                    )
                    ver_flag = (
                        "🔴" if info['with_ver'] == 0
                        else "✅"
                    )
                    print(
                        f"    {ver_flag} {modelo:<20} "
                        f"total={info['total']} "
                        f"ver={info['with_ver']} "
                        f"[{status_str}]"
                    )

            if dt['samples_without_version']:
                print(f"\n  📋 Ejemplos SIN versión:")
                for s in dt['samples_without_version'][:5]:
                    print(
                        f"    [{s['id']}] {s['modelo']} "
                        f"año={s['año']} "
                        f"status={s['status']}"
                    )
                    print(f"      URL: {s['url']}")

            if dt['samples_with_version']:
                print(f"\n  📋 Ejemplos CON versión:")
                for s in dt['samples_with_version'][:3]:
                    print(
                        f"    [{s['id']}] {s['modelo']} "
                        f"→ {s['version']} "
                        f"año={s['año']} "
                        f"status={s['status']}"
                    )

    # ── FIX RESULTS ──
    if fix_results:
        print(f"\n{'=' * 70}")
        print(f"🔧 RESULTADOS DEL FIX")
        print(f"{'=' * 70}")
        print(f"  Analizados: {fix_results['analyzed']}")
        print(f"  Corregidos: {fix_results['fixed']}")
        print(f"  Sin fix disponible: {fix_results['no_fix_available']}")
        print(f"  Errores: {fix_results['errors']}")

        if fix_results['examples']:
            print(f"\n  Ejemplos de correcciones:")
            for ex in fix_results['examples'][:10]:
                print(
                    f"    [{ex['id']}] {ex['marca']} "
                    f"{ex['modelo']} → version: "
                    f"'{ex['version_new']}'"
                )

    print("\n" + "=" * 70)


# ═══════════════════════════════════════════════════════════════
# 📄 GENERAR RESUMEN PARA GITHUB ACTIONS
# ═══════════════════════════════════════════════════════════════

def generate_github_summary(
    catalog_results: Dict,
    db_results: Dict,
    target_marca: str,
    fix_results: Dict = None,
):
    """Genera markdown para GITHUB_STEP_SUMMARY."""
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not summary_path:
        return

    lines = [
        f"# 🔍 Diagnóstico de Versiones",
        f"**Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Marca analizada:** `{target_marca or 'todas'}`",
        "",
        "## 📚 Catálogo vs BD - Versiones por marca",
        "",
        "| Marca | Modelos | Con Ver (cat) | "
        "Total BD | Con Ver (BD) | % Ver BD |",
        "|-------|---------|---------------|"
        "---------|--------------|----------|",
    ]

    brands_cat = catalog_results.get('brands', {})
    brands_db = db_results.get('by_brand', {})

    all_brands = sorted(
        set(list(brands_cat.keys()) + list(brands_db.keys())),
        key=lambda x: brands_db.get(x, {}).get('total', 0),
        reverse=True,
    )

    for marca in all_brands[:20]:
        cat = brands_cat.get(marca, {})
        db = brands_db.get(marca, {})
        flag = "🔴" if db.get('pct_version', 100) == 0 else ""

        lines.append(
            f"| {flag} {marca} | "
            f"{cat.get('modelos_catalog', '-')} | "
            f"{cat.get('modelos_con_versiones', '-')} | "
            f"{db.get('total', '-')} | "
            f"{db.get('con_version', '-')} | "
            f"{db.get('pct_version', '-')}% |"
        )

    if catalog_results.get('issues'):
        lines.extend([
            "",
            "## ⚠️ Problemas detectados",
            "",
        ])
        for issue in catalog_results['issues'][:10]:
            lines.append(f"- {issue}")

    if fix_results:
        lines.extend([
            "",
            "## 🔧 Resultados del fix",
            "",
            f"- Analizados: **{fix_results['analyzed']}**",
            f"- Corregidos: **{fix_results['fixed']}**",
            f"- Sin fix: **{fix_results['no_fix_available']}**",
        ])

    with open(summary_path, 'a') as f:
        f.write('\n'.join(lines))


# ═══════════════════════════════════════════════════════════════
# 🚀 MAIN
# ═══════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description='🔍 Diagnóstico de versiones por marca'
    )
    parser.add_argument(
        '--marca', default='chevrolet',
        help='Marca a analizar en detalle (default: chevrolet)'
    )
    parser.add_argument(
        '--db', default=DEFAULT_DB,
        help=f'Ruta a la BD (default: {DEFAULT_DB})'
    )
    parser.add_argument(
        '--dicts', default=DEFAULT_DICTS,
        help=f'Ruta al JSON (default: {DEFAULT_DICTS})'
    )
    parser.add_argument(
        '--fix', action='store_true',
        help='Intentar corregir versiones faltantes'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Simular fix sin aplicar cambios'
    )
    parser.add_argument(
        '--all-brands', action='store_true',
        help='Analizar todas las marcas sin target'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Más logs'
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.verbose)

    target = None if args.all_brands else args.marca

    logger.info(f"🔍 Analizando catálogo: {args.dicts}")
    catalog_results = analyze_catalog(args.dicts, target)

    logger.info(f"💾 Analizando BD: {args.db}")
    db_results = analyze_database(args.db, target)

    fix_results = None
    if args.fix:
        logger.info(f"🔧 Ejecutando fix (dry_run={args.dry_run})")
        fix_results = fix_missing_versions(
            db_path=args.db,
            dicts_path=args.dicts,
            target_marca=target,
            dry_run=args.dry_run,
        )

    print_full_report(catalog_results, db_results, target, fix_results)
    generate_github_summary(
        catalog_results, db_results, target, fix_results
    )

    # Exit code basado en severidad
    if catalog_results.get('issues'):
        critical = sum(
            1 for i in catalog_results['issues']
            if '🔴' in i or 'CRÍTICO' in i
        )
        if critical > 0:
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
