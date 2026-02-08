#!/usr/bin/env python3
"""
Script para consultar la base de datos de RosarioGarage.
Ejecutar: python query_database.py
"""

import sqlite3
import sys
from datetime import datetime, timedelta

DB_FILE = 'rosariogarage.db'


def main():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        print("=" * 70)
        print("🔍 CONSULTAS ROSARIOGARAGE DATABASE")
        print("=" * 70)
        print()
        
        # 1. Resumen general
        print("📊 RESUMEN GENERAL")
        print("-" * 70)
        
        total = cursor.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        activos = cursor.execute("SELECT COUNT(*) FROM vehicles WHERE activo = 1").fetchone()[0]
        inactivos = cursor.execute("SELECT COUNT(*) FROM vehicles WHERE activo = 0").fetchone()[0]
        
        print(f"Total vehículos:  {total:,}")
        print(f"├─ Activos:       {activos:,}")
        print(f"└─ Inactivos:     {inactivos:,}")
        print()
        
        # 2. Top marcas
        print("🏭 TOP 15 MARCAS (Activos)")
        print("-" * 70)
        
        for row in cursor.execute("""
            SELECT marca, COUNT(*) as cantidad,
                   ROUND(AVG(precio_usd), 0) as precio_promedio
            FROM vehicles 
            WHERE activo = 1 AND marca IS NOT NULL AND precio_usd IS NOT NULL
            GROUP BY marca 
            ORDER BY cantidad DESC 
            LIMIT 15
        """):
            print(f"{row['marca']:15s} {row['cantidad']:4,} unidades  |  Promedio: U$S {row['precio_promedio']:,.0f}")
        print()
        
        # 3. Cambios de precio últimos 7 días
        print("💰 CAMBIOS DE PRECIO (Últimos 7 días)")
        print("-" * 70)
        
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        
        for row in cursor.execute("""
            SELECT v.marca, v.modelo, v.año, ph.variacion_pct, ph.fecha, ph.precio_usd
            FROM price_history ph
            JOIN vehicles v ON ph.vehicle_id = v.id
            WHERE ph.fecha >= ? AND ph.variacion_pct IS NOT NULL
            ORDER BY ABS(ph.variacion_pct) DESC
            LIMIT 20
        """, (seven_days_ago,)):
            signo = "📉" if row['variacion_pct'] < 0 else "📈"
            print(f"{signo} {row['marca']} {row['modelo']} {row['año']} | "
                  f"{row['variacion_pct']:+.1f}% | U$S {row['precio_usd']:,.0f} | {row['fecha']}")
        print()
        
        # 4. Oportunidades: Mucho tiempo publicado + Precio bajó
        print("🎯 OPORTUNIDADES (>30 días publicado + Precio bajó)")
        print("-" * 70)
        
        for row in cursor.execute("""
            SELECT v.marca, v.modelo, v.año, v.precio_usd, v.dias_publicado,
                   (SELECT precio_usd FROM price_history 
                    WHERE vehicle_id = v.id 
                    ORDER BY fecha ASC LIMIT 1) as precio_inicial
            FROM vehicles v
            WHERE v.activo = 1 
            AND v.dias_publicado > 30
            AND v.precio_usd < (
                SELECT precio_usd FROM price_history 
                WHERE vehicle_id = v.id 
                ORDER BY fecha ASC LIMIT 1
            )
            ORDER BY v.dias_publicado DESC
            LIMIT 15
        """):
            baja = row['precio_inicial'] - row['precio_usd']
            baja_pct = (baja / row['precio_inicial']) * 100
            print(f"{row['marca']} {row['modelo']} {row['año']} | "
                  f"{row['dias_publicado']} días | "
                  f"Bajó U$S {baja:,.0f} ({baja_pct:.1f}%) | "
                  f"Ahora: U$S {row['precio_usd']:,.0f}")
        print()
        
        # 5. Vendidos recientemente
        print("🔴 VENDIDOS/INACTIVOS (Últimos 7 días)")
        print("-" * 70)
        
        for row in cursor.execute("""
            SELECT marca, modelo, año, precio_usd, dias_publicado, ultima_vista
            FROM vehicles
            WHERE activo = 0 AND ultima_vista >= date('now', '-7 days')
            ORDER BY ultima_vista DESC
            LIMIT 15
        """):
            print(f"{row['marca']} {row['modelo']} {row['año']} | "
                  f"{row['dias_publicado']} días publicado | "
                  f"U$S {row['precio_usd'] or 0:,.0f} | "
                  f"Último visto: {row['ultima_vista']}")
        print()
        
        # 6. Tiempo promedio de venta por marca
        print("⏱️ TIEMPO PROMEDIO DE VENTA POR MARCA (Top 10)")
        print("-" * 70)
        
        for row in cursor.execute("""
            SELECT marca, 
                   COUNT(*) as vendidos,
                   ROUND(AVG(dias_publicado), 1) as dias_promedio
            FROM vehicles 
            WHERE activo = 0 AND marca IS NOT NULL
            GROUP BY marca
            HAVING vendidos >= 5
            ORDER BY dias_promedio ASC
            LIMIT 10
        """):
            print(f"{row['marca']:15s} {row['dias_promedio']:5.1f} días promedio  |  {row['vendidos']} vendidos")
        print()
        
        # 7. Estadísticas de precios
        print("💵 ESTADÍSTICAS DE PRECIOS (Activos)")
        print("-" * 70)
        
        stats = cursor.execute("""
            SELECT 
                COUNT(*) as con_precio,
                ROUND(MIN(precio_usd), 0) as minimo,
                ROUND(AVG(precio_usd), 0) as promedio,
                ROUND(MAX(precio_usd), 0) as maximo
            FROM vehicles
            WHERE activo = 1 AND precio_usd IS NOT NULL
        """).fetchone()
        
        print(f"Vehículos con precio: {stats['con_precio']:,}")
        print(f"Mínimo:   U$S {stats['minimo']:>12,.0f}")
        print(f"Promedio: U$S {stats['promedio']:>12,.0f}")
        print(f"Máximo:   U$S {stats['maximo']:>12,.0f}")
        print()
        
        # 8. Metadata
        print("ℹ️ METADATA")
        print("-" * 70)
        
        for row in cursor.execute("SELECT key, value FROM scrape_metadata"):
            print(f"{row['key']:20s} {row['value']}")
        
        print("=" * 70)
        
        conn.close()
        
    except sqlite3.Error as e:
        print(f"❌ Error de base de datos: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"❌ No se encontró el archivo '{DB_FILE}'", file=sys.stderr)
        print("   Descárgalo desde GitHub Artifacts o Releases", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
