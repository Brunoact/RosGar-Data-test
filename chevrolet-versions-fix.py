#!/usr/bin/env python3
"""
Diagnóstico: ¿Por qué Chevrolet no tiene versiones?
"""
import json

with open("config/normalizer_dicts.json", "r", encoding="utf-8") as f:
    data = json.load(f)

catalog_index = data.get("catalog_index", {})
valid_versions = data.get("valid_versions", {})

print("=" * 70)
print("DIAGNÓSTICO DE VERSIONES POR MARCA")
print("=" * 70)

# 1. Check if chevrolet exists in catalog_index
print("\n🔍 Marcas en catalog_index:")
for marca in sorted(catalog_index.keys()):
    n_models = len(catalog_index[marca])
    print(f"  {marca}: {n_models} modelos")

# 2. Check valid_versions keys for each brand
print("\n🔍 Versiones por marca en valid_versions:")
brand_version_count = {}
for key, versions in valid_versions.items():
    brand = key.split("|")[0] if "|" in key else key
    brand = brand.strip()
    if brand not in brand_version_count:
        brand_version_count[brand] = {"keys": 0, "versions": 0, "examples": []}
    brand_version_count[brand]["keys"] += 1
    brand_version_count[brand]["versions"] += len(versions)
    if len(brand_version_count[brand]["examples"]) < 3:
        brand_version_count[brand]["examples"].append(
            (key, versions[:3])
        )

for brand in sorted(brand_version_count.keys()):
    info = brand_version_count[brand]
    print(f"\n  {brand}:")
    print(f"    Modelos con versiones: {info['keys']}")
    print(f"    Total versiones: {info['versions']}")
    for ex_key, ex_vers in info["examples"]:
        print(f"    Ejemplo: '{ex_key}' → {ex_vers}")

# 3. Specific Chevrolet check
print("\n" + "=" * 70)
print("🔎 ANÁLISIS ESPECÍFICO: CHEVROLET")
print("=" * 70)

# Find all chevrolet-like keys in catalog_index
chev_keys_catalog = [
    k for k in catalog_index.keys()
    if "chev" in k.lower() or "gm" in k.lower()
]
print(f"\nClaves en catalog_index que contienen 'chev' o 'gm': {chev_keys_catalog}")

if "chevrolet" in catalog_index:
    chev_models = list(catalog_index["chevrolet"].keys())
    print(f"\nModelos de chevrolet en catalog_index ({len(chev_models)}):")
    for m in sorted(chev_models):
        print(f"  - {m}")

# Find all chevrolet-like keys in valid_versions
chev_keys_versions = [
    k for k in valid_versions.keys()
    if "chev" in k.lower() or "gm" in k.lower()
]
print(f"\nClaves en valid_versions que contienen 'chev' o 'gm' ({len(chev_keys_versions)}):")
for k in sorted(chev_keys_versions):
    print(f"  '{k}' → {valid_versions[k][:5]}{'...' if len(valid_versions[k]) > 5 else ''}")

# 4. Cross-reference: models in catalog but NOT in valid_versions
if "chevrolet" in catalog_index:
    print(f"\n🔴 Modelos de Chevrolet SIN versiones:")
    missing = []
    has = []
    for model in sorted(catalog_index["chevrolet"].keys()):
        key = f"chevrolet|{model}"
        if key in valid_versions and len(valid_versions[key]) > 0:
            has.append(model)
        else:
            missing.append(model)
            # Check if exists with different key format
            similar = [
                k for k in valid_versions.keys()
                if model in k.lower() and "chev" in k.lower()
            ]
            if similar:
                print(f"  ❌ {model} → NO tiene key '{key}' PERO existe: {similar}")
            else:
                print(f"  ❌ {model} → NO tiene versiones")

    print(f"\n🟢 Modelos de Chevrolet CON versiones:")
    for model in has:
        key = f"chevrolet|{model}"
        print(f"  ✅ {model} → {len(valid_versions[key])} versiones")

    print(f"\n📊 Resumen Chevrolet: {len(has)} con versiones, {len(missing)} sin versiones")

# 5. Compare with another brand that DOES work
print("\n" + "=" * 70)
print("📊 COMPARACIÓN CON OTRA MARCA (ford)")
print("=" * 70)

if "ford" in catalog_index:
    ford_models = list(catalog_index["ford"].keys())
    ford_with_ver = sum(
        1 for m in ford_models
        if f"ford|{m}" in valid_versions and len(valid_versions[f"ford|{m}"]) > 0
    )
    print(f"  Ford: {len(ford_models)} modelos, {ford_with_ver} con versiones")

if "chevrolet" in catalog_index:
    chev_models_list = list(catalog_index["chevrolet"].keys())
    chev_with_ver = sum(
        1 for m in chev_models_list
        if f"chevrolet|{m}" in valid_versions and len(valid_versions[f"chevrolet|{m}"]) > 0
    )
    print(f"  Chevrolet: {len(chev_models_list)} modelos, {chev_with_ver} con versiones")

# 6. Check for whitespace/encoding issues in keys
print("\n🔍 Verificando problemas de encoding/whitespace en keys:")
for key in valid_versions.keys():
    if "chev" in key.lower():
        if key != key.strip():
            print(f"  ⚠️ Whitespace en: repr='{repr(key)}'")
        if "|" not in key:
            print(f"  ⚠️ Sin separador |: '{key}'")
        parts = key.split("|")
        if len(parts) == 2:
            if parts[0] != parts[0].strip() or parts[1] != parts[1].strip():
                print(f"  ⚠️ Whitespace en partes: '{repr(parts[0])}' | '{repr(parts[1])}'")
