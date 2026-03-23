# debug_single.py
# Ejecutar: python debug_single.py
import sqlite3
import json
import sys

sys.path.insert(0, '.')
import normalizer_v2 as nv2

DB = 'rosariogarage.db'
DICTS = 'config/normalizer_dicts.json'

# Inicializar
loaded = nv2.init_normalizer(dicts_path=DICTS)
print(f"Catálogo cargado: {loaded}")

catalog = nv2.get_catalog()

# ── Verificar que cruze existe en catálogo ──
print("\n=== CATÁLOGO ===")
print(f"¿Tiene chevrolet? {catalog.has_brand('chevrolet')}")
print(f"¿Tiene cruze? {catalog.has_model('chevrolet', 'cruze')}")
versiones = catalog.get_versions('chevrolet', 'cruze')
print(f"Versiones de cruze: {versiones}")

# ── Leer un registro real de la BD ──
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, marca, modelo, version, version_raw,
           descripcion, año, kilometros
    FROM vehicles
    WHERE marca = 'chevrolet'
      AND modelo = 'cruze'
      AND (version IS NULL OR version = '')
    LIMIT 5
""").fetchall()

print(f"\n=== REGISTROS EN BD ===")
for row in rows:
    row = dict(row)
    print(f"\n[{row['id']}]")
    print(f"  marca:       {row['marca']}")
    print(f"  modelo:      {row['modelo']}")
    print(f"  version:     {row['version']}")
    print(f"  version_raw: {row['version_raw']}")
    print(f"  descripcion: {str(row['descripcion'])[:100]}")
    print(f"  año:         {row['año']}")

    # Probar normalización
    print(f"\n  → Probando normalize_vehicle:")
    norm = nv2.normalize_vehicle(
        titulo='',
        descripcion=row['descripcion'] or '',
        marca_raw=row['marca'] or '',
        modelo_raw=row['modelo'] or '',
        version_raw=row['version_raw'] or row['version'] or '',
        año_raw=row['año'],
        km_raw=row['kilometros'],
    )
    print(f"    marca:        {norm['marca']}")
    print(f"    modelo:       {norm['modelo']}")
    print(f"    version:      {norm['version']}")
    print(f"    norm_status:  {norm['norm_status']}")
    print(f"    confidence:   {norm['confidence']}")
    print(f"    ver_method:   {norm['version_method']}")
    print(f"    ver_score:    {norm['version_score']}")
    if norm['warnings']:
        print(f"    warnings:     {norm['warnings']}")

conn.close()

# ── Probar directamente con texto conocido ──
print("\n=== PRUEBA DIRECTA ===")
casos = [
    {
        'marca': 'chevrolet',
        'modelo': 'cruze',
        'version_raw': 'LT 1.4 Turbo Automático',
        'desc': ''
    },
    {
        'marca': 'chevrolet',
        'modelo': 'cruze',
        'version_raw': 'LTZ',
        'desc': ''
    },
    {
        'marca': 'chevrolet',
        'modelo': 'onix',
        'version_raw': 'LT',
        'desc': ''
    },
    {
        'marca': 'chevrolet',
        'modelo': 'tracker',
        'version_raw': 'LTZ 1.8 automatica',
        'desc': ''
    },
]

for c in casos:
    norm = nv2.normalize_vehicle(
        titulo='',
        descripcion=c['desc'],
        marca_raw=c['marca'],
        modelo_raw=c['modelo'],
        version_raw=c['version_raw'],
    )
    status = "✅" if norm['version'] else "❌"
    print(
        f"  {status} {c['marca']} {c['modelo']} "
        f"'{c['version_raw']}' → "
        f"version='{norm['version']}' "
        f"({norm['version_method']}, "
        f"conf:{norm['confidence']})"
    )
