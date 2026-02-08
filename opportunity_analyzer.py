#!/usr/bin/env python3
"""
🎯 OPPORTUNITY ANALYZER - Sistema de Detección de Oportunidades
==============================================================

Analiza la base de datos de vehículos para identificar:
1. Modelos con alto spread (margen de arbitraje)
2. Vehículos específicos subvaluados
3. Señales de vendedores motivados

Uso:
    python opportunity_analyzer.py [--report] [--csv] [--debug]
"""

import sqlite3
import json
import os
import sys
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
import statistics
import re
import argparse

# ═══════════════════════════════════════════════════════════════
# 📊 CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

DB_PATH = 'rosariogarage.db'
CONFIG_DIR = 'config'
REPORTS_DIR = 'reports'

@dataclass
class AnalysisConfig:
    # Filtrado
    min_price_usd: float = 1000
    max_price_usd: float = 500000
    min_year: int = 1990
    max_year: int = 2025
    min_samples: int = 5
    
    # Estadísticas
    spread_threshold_high: float = 15.0  # % spread para considerar "alto"
    volume_threshold_high: int = 10      # unidades para considerar "alto volumen"
    anomaly_threshold: float = 15.0      # % bajo Q1 para ser anomalía
    
    # Scoring weights
    w_price_discount: float = 0.35
    w_days_published: float = 0.20
    w_price_drops: float = 0.20
    w_seller_type: float = 0.10
    w_model_liquidity: float = 0.15
    
    # Pricing
    first_offer_discount: float = 0.15   # 15% descuento para primera oferta
    target_discount: float = 0.08        # 8% descuento objetivo
    max_price_margin: float = 0.05       # 5% margen mínimo
    costs_percent: float = 0.03          # 3% costos estimados


def load_config() -> Tuple[AnalysisConfig, Dict, List[str], List[str]]:
    """Carga configuración desde archivos JSON."""
    config = AnalysisConfig()
    brand_aliases = {}
    urgency_keywords = []
    agency_keywords = []
    
    # Cargar config de análisis
    config_path = os.path.join(CONFIG_DIR, 'analysis_config.json')
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
            if 'filtering' in data:
                config.min_price_usd = data['filtering'].get('min_price_usd', 1000)
                config.max_price_usd = data['filtering'].get('max_price_usd', 500000)
                config.min_year = data['filtering'].get('min_year', 1990)
                config.max_year = data['filtering'].get('max_year', 2025)
                config.min_samples = data['filtering'].get('min_samples_for_stats', 5)
            
            if 'statistics' in data:
                config.spread_threshold_high = data['statistics'].get('spread_threshold_high', 15)
                config.volume_threshold_high = data['statistics'].get('volume_threshold_high', 10)
                config.anomaly_threshold = data['statistics'].get('anomaly_threshold_percent', 15)
            
            if 'scoring' in data:
                config.w_price_discount = data['scoring'].get('weight_price_discount', 0.35)
                config.w_days_published = data['scoring'].get('weight_days_published', 0.20)
                config.w_price_drops = data['scoring'].get('weight_price_drops', 0.20)
                config.w_seller_type = data['scoring'].get('weight_seller_type', 0.10)
                config.w_model_liquidity = data['scoring'].get('weight_model_liquidity', 0.15)
            
            if 'pricing' in data:
                config.first_offer_discount = data['pricing'].get('first_offer_discount', 0.15)
                config.target_discount = data['pricing'].get('target_price_discount', 0.08)
                config.max_price_margin = data['pricing'].get('max_price_margin', 0.05)
                config.costs_percent = data['pricing'].get('estimated_costs_percent', 0.03)
            
            urgency_keywords = data.get('urgency_keywords', [])
            agency_keywords = data.get('agency_keywords', [])
    
    # Cargar aliases de marcas
    aliases_path = os.path.join(CONFIG_DIR, 'brand_aliases.json')
    if os.path.exists(aliases_path):
        with open(aliases_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            brand_aliases = data.get('brand_aliases', {})
    
    return config, brand_aliases, urgency_keywords, agency_keywords


# ═══════════════════════════════════════════════════════════════
# 📊 ESTRUCTURAS DE DATOS
# ═══════════════════════════════════════════════════════════════

@dataclass
class Vehicle:
    """Representa un vehículo de la base de datos."""
    id: str
    url: str
    marca: str
    modelo: str
    version: Optional[str]
    año: Optional[int]
    kilometros: int
    precio_usd: float
    precio_ars: float
    agencia: Optional[str]
    dias_publicado: int
    primera_vista: str
    ultima_vista: str
    
    # Campos calculados
    marca_normalizada: str = ""
    modelo_normalizado: str = ""
    km_por_año: float = 0.0
    es_agencia: bool = False
    
    def __post_init__(self):
        if self.año and self.año > 0:
            años_uso = max(1, datetime.now().year - self.año)
            self.km_por_año = self.kilometros / años_uso if self.kilometros else 0


@dataclass
class ModelStats:
    """Estadísticas de mercado para un modelo específico."""
    marca: str
    modelo: str
    año_min: int
    año_max: int
    
    # Volumen
    count: int = 0
    
    # Estadísticas de precio
    precio_min: float = 0
    precio_max: float = 0
    precio_mean: float = 0
    precio_median: float = 0
    precio_q1: float = 0  # Percentil 25
    precio_q3: float = 0  # Percentil 75
    precio_iqr: float = 0  # Rango intercuartílico
    
    # Spread (margen de arbitraje teórico)
    spread_percent: float = 0  # (Mediana - Q1) / Q1 * 100
    
    # Clasificación
    cuadrante: str = ""  # "oro", "clavo_rentable", "commodity", "evitar"
    
    # Liquidez estimada
    dias_promedio_publicado: float = 0
    
    @property
    def key(self) -> str:
        return f"{self.marca}|{self.modelo}|{self.año_min}-{self.año_max}"


@dataclass 
class Opportunity:
    """Representa una oportunidad de compra detectada."""
    vehicle: Vehicle
    model_stats: ModelStats
    
    # Análisis de precio
    descuento_vs_q1: float = 0       # % por debajo de Q1
    descuento_vs_median: float = 0   # % por debajo de mediana
    
    # Señales de motivación
    tiene_keywords_urgencia: bool = False
    keywords_encontradas: List[str] = field(default_factory=list)
    precio_bajo_historico: int = 0   # Número de bajadas de precio
    
    # Scoring
    score: float = 0                 # 0-100
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    
    # Precios sugeridos
    precio_primera_oferta: float = 0
    precio_objetivo: float = 0
    precio_maximo: float = 0
    margen_estimado: float = 0       # % de ganancia estimada
    
    # Evaluación de factibilidad
    factibilidad: str = ""           # "alta", "media", "baja"
    notas: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 🔧 NORMALIZACIÓN Y LIMPIEZA
# ═══════════════════════════════════════════════════════════════

class DataNormalizer:
    """Normaliza y limpia los datos de vehículos."""
    
    def __init__(self, brand_aliases: Dict[str, str], config: AnalysisConfig):
        self.brand_aliases = {k.lower(): v for k, v in brand_aliases.items()}
        self.config = config
    
    def normalize_brand(self, marca: str) -> str:
        """Normaliza el nombre de la marca."""
        if not marca:
            return "Desconocido"
        
        marca_lower = marca.lower().strip()
        
        # Buscar en aliases
        if marca_lower in self.brand_aliases:
            return self.brand_aliases[marca_lower]
        
        # Capitalizar correctamente
        return marca.strip().title()
    
    def normalize_model(self, modelo: str, marca: str) -> str:
        """Normaliza el nombre del modelo."""
        if not modelo or modelo == 'N/A':
            return "Otro"
        
        modelo = modelo.strip()
        
        # Remover la marca del modelo si está repetida
        marca_lower = marca.lower()
        if modelo.lower().startswith(marca_lower):
            modelo = modelo[len(marca):].strip()
        
        # Normalizar versiones comunes
        modelo = re.sub(r'\s+', ' ', modelo)  # Espacios múltiples
        
        # Extraer modelo base (primera palabra significativa)
        # Esto ayuda a agrupar variantes del mismo modelo
        palabras = modelo.split()
        if palabras:
            modelo_base = palabras[0]
            # Si la segunda palabra es un número, incluirla (ej: "Gol 1.6")
            if len(palabras) > 1 and re.match(r'^[\d\.]+$', palabras[1]):
                modelo_base = f"{palabras[0]} {palabras[1]}"
            return modelo_base
        
        return modelo
    
    def is_valid_vehicle(self, v: Vehicle) -> bool:
        """Verifica si el vehículo tiene datos válidos para análisis."""
        # Precio válido
        if not v.precio_usd or v.precio_usd < self.config.min_price_usd:
            return False
        if v.precio_usd > self.config.max_price_usd:
            return False
        
        # Año válido
        if v.año:
            if v.año < self.config.min_year or v.año > self.config.max_year:
                return False
        
        # Marca válida
        if not v.marca or v.marca in ['N/A', 'Desconocido', '']:
            return False
        
        return True
    
    def detect_agency(self, agencia: str, agency_keywords: List[str]) -> bool:
        """Detecta si es una agencia o particular."""
        if not agencia:
            return False
        
        agencia_lower = agencia.lower()
        for keyword in agency_keywords:
            if keyword.lower() in agencia_lower:
                return True
        
        return False


# ═══════════════════════════════════════════════════════════════
# 📈 ANÁLISIS ESTADÍSTICO
# ═══════════════════════════════════════════════════════════════

class MarketAnalyzer:
    """Calcula estadísticas de mercado por modelo."""
    
    def __init__(self, config: AnalysisConfig):
        self.config = config
    
    def calculate_model_stats(
        self, 
        vehicles: List[Vehicle],
        year_ranges: List[Tuple[int, int]] = None
    ) -> Dict[str, ModelStats]:
        """
        Calcula estadísticas para cada combinación marca-modelo.
        
        Args:
            vehicles: Lista de vehículos normalizados
            year_ranges: Lista de rangos de años, ej: [(2010, 2015), (2016, 2020)]
                        Si es None, agrupa todos los años juntos
        """
        if year_ranges is None:
            # Agrupar por décadas automáticamente
            year_ranges = [
                (1990, 1999),
                (2000, 2009),
                (2010, 2015),
                (2016, 2020),
                (2021, 2025),
            ]
        
        # Agrupar vehículos
        groups: Dict[str, List[Vehicle]] = defaultdict(list)
        
        for v in vehicles:
            if not v.año:
                continue
            
            # Encontrar el rango de año correspondiente
            for año_min, año_max in year_ranges:
                if año_min <= v.año <= año_max:
                    key = f"{v.marca_normalizada}|{v.modelo_normalizado}|{año_min}-{año_max}"
                    groups[key].append(v)
                    break
        
        # Calcular estadísticas por grupo
        stats_dict: Dict[str, ModelStats] = {}
        
        for key, group_vehicles in groups.items():
            if len(group_vehicles) < self.config.min_samples:
                continue
            
            parts = key.split('|')
            marca = parts[0]
            modelo = parts[1]
            años = parts[2].split('-')
            año_min = int(años[0])
            año_max = int(años[1])
            
            precios = sorted([v.precio_usd for v in group_vehicles])
            dias = [v.dias_publicado for v in group_vehicles]
            
            stats = ModelStats(
                marca=marca,
                modelo=modelo,
                año_min=año_min,
                año_max=año_max,
                count=len(group_vehicles),
                precio_min=min(precios),
                precio_max=max(precios),
                precio_mean=statistics.mean(precios),
                precio_median=statistics.median(precios),
                dias_promedio_publicado=statistics.mean(dias) if dias else 0
            )
            
            # Calcular percentiles
            n = len(precios)
            stats.precio_q1 = precios[n // 4] if n >= 4 else precios[0]
            stats.precio_q3 = precios[3 * n // 4] if n >= 4 else precios[-1]
            stats.precio_iqr = stats.precio_q3 - stats.precio_q1
            
            # Calcular spread (margen de arbitraje teórico)
            if stats.precio_q1 > 0:
                stats.spread_percent = (
                    (stats.precio_median - stats.precio_q1) / stats.precio_q1
                ) * 100
            
            # Clasificar en cuadrante
            stats.cuadrante = self._classify_quadrant(stats)
            
            stats_dict[key] = stats
        
        return stats_dict
    
    def _classify_quadrant(self, stats: ModelStats) -> str:
        """Clasifica el modelo en un cuadrante según spread y volumen."""
        high_spread = stats.spread_percent >= self.config.spread_threshold_high
        high_volume = stats.count >= self.config.volume_threshold_high
        
        if high_spread and high_volume:
            return "oro"              # 🥇 Oportunidades frecuentes y rentables
        elif high_spread and not high_volume:
            return "clavo_rentable"   # 💎 Raras pero muy rentables
        elif not high_spread and high_volume:
            return "commodity"        # 📦 Poco margen pero fácil vender
        else:
            return "evitar"           # ⚠️ Poco margen y difícil vender


# ═══════════════════════════════════════════════════════════════
# 🎯 DETECCIÓN DE OPORTUNIDADES
# ═══════════════════════════════════════════════════════════════

class OpportunityDetector:
    """Detecta vehículos subvaluados y oportunidades de compra."""
    
    def __init__(
        self, 
        config: AnalysisConfig,
        urgency_keywords: List[str]
    ):
        self.config = config
        self.urgency_keywords = [k.lower() for k in urgency_keywords]
    
    def find_anomalies(
        self,
        vehicles: List[Vehicle],
        model_stats: Dict[str, ModelStats],
        price_history: Dict[str, List[Tuple[str, float]]]  # vehicle_id -> [(fecha, precio)]
    ) -> List[Opportunity]:
        """
        Encuentra vehículos con precios anormalmente bajos.
        """
        opportunities = []
        
        for v in vehicles:
            # Buscar estadísticas del modelo
            stats_key = None
            for key, stats in model_stats.items():
                if (stats.marca == v.marca_normalizada and 
                    stats.modelo == v.modelo_normalizado and
                    v.año and stats.año_min <= v.año <= stats.año_max):
                    stats_key = key
                    break
            
            if not stats_key:
                continue
            
            stats = model_stats[stats_key]
            
            # Calcular descuento vs Q1
            if stats.precio_q1 <= 0:
                continue
            
            descuento_vs_q1 = (
                (stats.precio_q1 - v.precio_usd) / stats.precio_q1
            ) * 100
            
            # ¿Es una anomalía? (precio significativamente bajo)
            if descuento_vs_q1 < self.config.anomaly_threshold:
                continue
            
            # Crear oportunidad
            opp = Opportunity(
                vehicle=v,
                model_stats=stats,
                descuento_vs_q1=descuento_vs_q1,
                descuento_vs_median=(
                    (stats.precio_median - v.precio_usd) / stats.precio_median
                ) * 100 if stats.precio_median > 0 else 0
            )
            
            # Buscar señales de urgencia
            # (Nota: necesitaríamos la descripción, por ahora usamos agencia)
            if v.agencia:
                opp.keywords_encontradas = self._find_urgency_keywords(v.agencia)
                opp.tiene_keywords_urgencia = len(opp.keywords_encontradas) > 0
            
            # Contar bajadas de precio
            if v.id in price_history:
                opp.precio_bajo_historico = len(price_history[v.id])
            
            # Calcular precios sugeridos
            self._calculate_suggested_prices(opp, stats)
            
            # Calcular score
            opp.score, opp.score_breakdown = self._calculate_score(opp, stats)
            
            # Evaluar factibilidad
            opp.factibilidad, opp.notas = self._evaluate_feasibility(opp)
            
            opportunities.append(opp)
        
        # Ordenar por score descendente
        opportunities.sort(key=lambda x: x.score, reverse=True)
        
        return opportunities
    
    def _find_urgency_keywords(self, text: str) -> List[str]:
        """Busca palabras clave de urgencia en el texto."""
        if not text:
            return []
        
        text_lower = text.lower()
        found = []
        
        for keyword in self.urgency_keywords:
            if keyword in text_lower:
                found.append(keyword)
        
        return found
    
    def _calculate_suggested_prices(self, opp: Opportunity, stats: ModelStats):
        """Calcula los precios sugeridos para negociación."""
        precio_actual = opp.vehicle.precio_usd
        
        # Primera oferta: agresiva
        opp.precio_primera_oferta = round(
            precio_actual * (1 - self.config.first_offer_discount), 
            -2  # Redondear a centenas
        )
        
        # Precio objetivo: realista
        opp.precio_objetivo = round(
            precio_actual * (1 - self.config.target_discount),
            -2
        )
        
        # Precio máximo: garantiza margen mínimo
        # Asumiendo que vendes en la mediana
        margen_minimo = self.config.max_price_margin + self.config.costs_percent
        opp.precio_maximo = round(
            stats.precio_median * (1 - margen_minimo),
            -2
        )
        
        # Asegurar que precio máximo no supere el actual
        if opp.precio_maximo > precio_actual:
            opp.precio_maximo = precio_actual
        
        # Calcular margen estimado (si compras al objetivo y vendes en mediana)
        if opp.precio_objetivo > 0:
            opp.margen_estimado = (
                (stats.precio_median - opp.precio_objetivo) / opp.precio_objetivo
            ) * 100 - (self.config.costs_percent * 100)
    
    def _calculate_score(
        self, 
        opp: Opportunity, 
        stats: ModelStats
    ) -> Tuple[float, Dict[str, float]]:
        """
        Calcula un score de 0-100 para la oportunidad.
        Combina múltiples factores ponderados.
        """
        breakdown = {}
        
        # 1. Descuento vs precio de mercado (0-100)
        # Más descuento = mejor
        score_descuento = min(100, opp.descuento_vs_q1 * 3)  # 33% descuento = 100 pts
        breakdown['descuento'] = score_descuento * self.config.w_price_discount
        
        # 2. Días publicado (0-100)
        # Más días = más negociable
        dias = opp.vehicle.dias_publicado
        if dias >= 60:
            score_dias = 100
        elif dias >= 30:
            score_dias = 70
        elif dias >= 14:
            score_dias = 40
        else:
            score_dias = 20
        breakdown['dias_publicado'] = score_dias * self.config.w_days_published
        
        # 3. Bajadas de precio históricas (0-100)
        bajadas = opp.precio_bajo_historico
        score_bajadas = min(100, bajadas * 30)  # 3+ bajadas = 100 pts
        breakdown['bajadas_precio'] = score_bajadas * self.config.w_price_drops
        
        # 4. Tipo de vendedor (0-100)
        # Particulares más negociables que agencias
        score_vendedor = 30 if opp.vehicle.es_agencia else 80
        if opp.tiene_keywords_urgencia:
            score_vendedor = 100
        breakdown['tipo_vendedor'] = score_vendedor * self.config.w_seller_type
        
        # 5. Liquidez del modelo (0-100)
        # Más unidades = más fácil revender
        if stats.count >= 20:
            score_liquidez = 100
        elif stats.count >= 10:
            score_liquidez = 70
        elif stats.count >= 5:
            score_liquidez = 40
        else:
            score_liquidez = 20
        breakdown['liquidez'] = score_liquidez * self.config.w_model_liquidity
        
        # Score total
        total = sum(breakdown.values())
        
        return round(total, 1), breakdown
    
    def _evaluate_feasibility(self, opp: Opportunity) -> Tuple[str, List[str]]:
        """Evalúa la factibilidad de negociación."""
        notas = []
        score_factibilidad = 0
        
        # Días publicado
        if opp.vehicle.dias_publicado >= 30:
            score_factibilidad += 30
            notas.append("⏰ +30 días publicado - vendedor probablemente flexible")
        elif opp.vehicle.dias_publicado >= 14:
            score_factibilidad += 15
            notas.append("📅 2+ semanas publicado")
        
        # Tipo de vendedor
        if not opp.vehicle.es_agencia:
            score_factibilidad += 25
            notas.append("👤 Vendedor particular - más negociable")
        else:
            notas.append("🏢 Agencia - menos margen de negociación")
        
        # Keywords de urgencia
        if opp.tiene_keywords_urgencia:
            score_factibilidad += 30
            notas.append(f"🔥 Señales de urgencia: {', '.join(opp.keywords_encontradas)}")
        
        # Historial de bajadas
        if opp.precio_bajo_historico >= 2:
            score_factibilidad += 20
            notas.append(f"📉 Ya bajó el precio {opp.precio_bajo_historico} veces")
        
        # Descuento que estamos pidiendo
        descuento_primera_oferta = (
            (opp.vehicle.precio_usd - opp.precio_primera_oferta) / opp.vehicle.precio_usd
        ) * 100
        
        if descuento_primera_oferta > 20:
            notas.append(f"⚠️ Primera oferta pide {descuento_primera_oferta:.0f}% de descuento - puede ser agresivo")
        
        # Clasificar
        if score_factibilidad >= 60:
            return "alta", notas
        elif score_factibilidad >= 30:
            return "media", notas
        else:
            return "baja", notas


# ═══════════════════════════════════════════════════════════════
# 📋 GENERACIÓN DE REPORTES
# ═══════════════════════════════════════════════════════════════

class ReportGenerator:
    """Genera reportes en diferentes formatos."""
    
    def __init__(self, output_dir: str = REPORTS_DIR):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
    
    def generate_market_analysis(
        self, 
        model_stats: Dict[str, ModelStats],
        dolar_mep: float
    ) -> str:
        """Genera reporte de análisis de mercado."""
        
        # Separar por cuadrante
        oro = [s for s in model_stats.values() if s.cuadrante == "oro"]
        clavos = [s for s in model_stats.values() if s.cuadrante == "clavo_rentable"]
        commodity = [s for s in model_stats.values() if s.cuadrante == "commodity"]
        evitar = [s for s in model_stats.values() if s.cuadrante == "evitar"]
        
        # Ordenar por spread
        oro.sort(key=lambda x: x.spread_percent, reverse=True)
        clavos.sort(key=lambda x: x.spread_percent, reverse=True)
        
        report = f"""# 📊 Análisis de Mercado - RosarioGarage

**Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  
**Dólar MEP:** ${dolar_mep:,.2f}  
**Modelos analizados:** {len(model_stats)}

---

## 🎯 Matriz de Oportunidades

|                    | 📈 ALTO SPREAD (>{self.get_config_value('spread_threshold_high')}%) | 📉 BAJO SPREAD |
|--------------------|---------------------------|----------------|
| **📦 Alto Volumen** | 🥇 ORO ({len(oro)})        | 📦 Commodity ({len(commodity)}) |
| **🔍 Bajo Volumen** | 💎 Clavos ({len(clavos)})  | ⚠️ Evitar ({len(evitar)}) |

---

## 🥇 Modelos ORO (Alto Spread + Alto Volumen)

*Oportunidades frecuentes con buen margen de arbitraje*

| Marca | Modelo | Años | Unidades | Q1 (USD) | Mediana | Spread % |
|-------|--------|------|----------|----------|---------|----------|
"""
        for s in oro[:20]:
            report += f"| {s.marca} | {s.modelo} | {s.año_min}-{s.año_max} | {s.count} | ${s.precio_q1:,.0f} | ${s.precio_median:,.0f} | **{s.spread_percent:.1f}%** |\n"
        
        report += f"""
---

## 💎 Clavos Rentables (Alto Spread + Bajo Volumen)

*Oportunidades raras pero muy rentables - requieren paciencia*

| Marca | Modelo | Años | Unidades | Q1 (USD) | Mediana | Spread % |
|-------|--------|------|----------|----------|---------|----------|
"""
        for s in clavos[:15]:
            report += f"| {s.marca} | {s.modelo} | {s.año_min}-{s.año_max} | {s.count} | ${s.precio_q1:,.0f} | ${s.precio_median:,.0f} | **{s.spread_percent:.1f}%** |\n"
        
        report += f"""
---

## 📦 Modelos Commodity (Bajo Spread + Alto Volumen)

*Fácil de vender pero poco margen - solo si consigues MUY barato*

| Marca | Modelo | Años | Unidades | Q1 (USD) | Mediana | Spread % |
|-------|--------|------|----------|----------|---------|----------|
"""
        for s in sorted(commodity, key=lambda x: x.count, reverse=True)[:10]:
            report += f"| {s.marca} | {s.modelo} | {s.año_min}-{s.año_max} | {s.count} | ${s.precio_q1:,.0f} | ${s.precio_median:,.0f} | {s.spread_percent:.1f}% |\n"
        
        report += """
---

## 📌 Cómo Usar Este Reporte

1. **Modelos ORO**: Prioriza buscar estos modelos en Marketplace para comparar precios reales de venta
2. **Clavos Rentables**: Ten alertas activas para estos modelos - cuando aparezcan baratos, actúa rápido
3. **Commodity**: Solo compra si el descuento es excepcional (>25% bajo Q1)
4. **Evitar**: No pierdas tiempo con estos modelos

### 🔍 Próximos Pasos

1. Toma los **Top 10 modelos ORO** de este reporte
2. Busca en Facebook Marketplace publicaciones **vendidas** de estos modelos
3. Registra los precios reales de venta (no de lista)
4. Con esa info, ajusta los parámetros de compra

"""
        
        # Guardar
        path = os.path.join(self.output_dir, 'MARKET_ANALYSIS.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(report)
        
        return path
    
    def generate_opportunities_report(
        self,
        opportunities: List[Opportunity],
        dolar_mep: float
    ) -> str:
        """Genera reporte de oportunidades específicas."""
        
        # Filtrar y ordenar
        high_score = [o for o in opportunities if o.score >= 60]
        medium_score = [o for o in opportunities if 40 <= o.score < 60]
        
        report = f"""# 🎯 Oportunidades de Compra Detectadas

**Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  
**Dólar MEP:** ${dolar_mep:,.2f}  
**Total oportunidades:** {len(opportunities)}

---

## 🔥 Oportunidades TOP (Score ≥60)

"""
        if high_score:
            for i, opp in enumerate(high_score[:20], 1):
                v = opp.vehicle
                s = opp.model_stats
                
                report += f"""### {i}. {v.marca} {v.modelo} {v.año or 'S/A'}

| Dato | Valor |
|------|-------|
| **Score** | **{opp.score:.0f}/100** |
| **Precio publicado** | USD ${v.precio_usd:,.0f} |
| **Q1 del modelo** | USD ${s.precio_q1:,.0f} |
| **Mediana del modelo** | USD ${s.precio_median:,.0f} |
| **Descuento vs Q1** | **{opp.descuento_vs_q1:.1f}%** |
| **Días publicado** | {v.dias_publicado} días |
| **Kilómetros** | {v.kilometros:,} km |
| **Factibilidad** | {opp.factibilidad.upper()} |

**💰 Precios Sugeridos:**
- Primera oferta: **USD ${opp.precio_primera_oferta:,.0f}** (agresiva)
- Precio objetivo: **USD ${opp.precio_objetivo:,.0f}** (realista)
- Precio máximo: **USD ${opp.precio_maximo:,.0f}** (límite)
- Margen estimado: **{opp.margen_estimado:.1f}%**

**📝 Notas:**
"""
                for nota in opp.notas:
                    report += f"- {nota}\n"
                
                report += f"\n🔗 [Ver publicación]({v.url})\n\n---\n\n"
        else:
            report += "*No se encontraron oportunidades con score ≥60*\n\n"
        
        report += f"""
## ⚡ Oportunidades Medias (Score 40-59)

| # | Vehículo | Precio | Desc. vs Q1 | Score | Link |
|---|----------|--------|-------------|-------|------|
"""
        for i, opp in enumerate(medium_score[:30], 1):
            v = opp.vehicle
            report += f"| {i} | {v.marca} {v.modelo} {v.año or ''} | ${v.precio_usd:,.0f} | {opp.descuento_vs_q1:.0f}% | {opp.score:.0f} | [Ver]({v.url}) |\n"
        
        report += """
---

## 📌 Cómo Interpretar

- **Score ≥60**: Contactar hoy - alta probabilidad de buen negocio
- **Score 40-59**: Evaluar caso por caso - pueden ser buenos con negociación
- **Factibilidad Alta**: Vendedor probablemente aceptará ofertas
- **Margen Estimado**: Ganancia esperada si compras al objetivo y vendes en mediana

### ⚠️ Recordatorios

1. **Siempre inspecciona** el vehículo antes de comprar
2. **Verifica documentación** (título, deudas, multas)
3. Los precios son en USD y pueden variar con el dólar
4. El margen estimado no incluye costos de reparación

"""
        
        # Guardar
        path = os.path.join(self.output_dir, 'OPPORTUNITIES.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(report)
        
        return path
    
    def generate_models_csv(
        self,
        model_stats: Dict[str, ModelStats]
    ) -> str:
        """Genera CSV con modelos para investigar en Marketplace."""
        
        # Ordenar por prioridad (oro primero, luego clavos, ordenados por spread)
        models = list(model_stats.values())
        
        def priority_key(s: ModelStats) -> Tuple[int, float]:
            if s.cuadrante == "oro":
                return (0, -s.spread_percent)
            elif s.cuadrante == "clavo_rentable":
                return (1, -s.spread_percent)
            elif s.cuadrante == "commodity":
                return (2, -s.count)
            else:
                return (3, 0)
        
        models.sort(key=priority_key)
        
        # Generar CSV
        lines = [
            "Prioridad,Cuadrante,Marca,Modelo,Años,Unidades,Q1_USD,Mediana_USD,Spread_Pct,Dias_Prom,Precio_Venta_FB,Notas"
        ]
        
        for i, s in enumerate(models[:50], 1):  # Top 50
            line = f'{i},{s.cuadrante},{s.marca},{s.modelo},{s.año_min}-{s.año_max},{s.count},{s.precio_q1:.0f},{s.precio_median:.0f},{s.spread_percent:.1f},{s.dias_promedio_publicado:.0f},,'
            lines.append(line)
        
        path = os.path.join(self.output_dir, 'models_to_research.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        
        return path
    
    def get_config_value(self, key: str) -> Any:
        """Helper para obtener valores de config."""
        config = AnalysisConfig()
        return getattr(config, key, 'N/A')


# ═══════════════════════════════════════════════════════════════
# 🚀 EJECUCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════

def load_data_from_db(db_path: str) -> Tuple[List[Dict], Dict[str, List], float]:
    """Carga datos de la base de datos."""
    if not os.path.exists(db_path):
        print(f"❌ No se encontró la base de datos: {db_path}")
        sys.exit(1)
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Cargar vehículos activos
    vehicles = []
    for row in conn.execute("""
        SELECT id, url, marca, modelo, version, año, kilometros,
               precio_usd, precio_ars, agencia, dias_publicado,
               primera_vista, ultima_vista
        FROM vehicles
        WHERE activo = 1
    """):
        vehicles.append(dict(row))
    
    # Cargar historial de precios
    price_history = defaultdict(list)
    for row in conn.execute("""
        SELECT vehicle_id, fecha, precio_usd
        FROM price_history
        WHERE variacion_pct IS NOT NULL
        ORDER BY vehicle_id, fecha
    """):
        price_history[row['vehicle_id']].append((row['fecha'], row['precio_usd']))
    
    # Cargar dólar
    dolar = conn.execute(
        "SELECT value FROM scrape_metadata WHERE key = 'last_dolar_mep'"
    ).fetchone()
    dolar_mep = float(dolar[0]) if dolar else 1000.0
    
    conn.close()
    
    return vehicles, dict(price_history), dolar_mep


def main():
    """Función principal."""
    parser = argparse.ArgumentParser(description='Analizador de Oportunidades de Compra')
    parser.add_argument('--report', action='store_true', help='Generar reportes markdown')
    parser.add_argument('--csv', action='store_true', help='Generar CSV para investigación')
    parser.add_argument('--debug', action='store_true', help='Modo debug con más información')
    parser.add_argument('--db', default=DB_PATH, help='Ruta a la base de datos')
    args = parser.parse_args()
    
    print("=" * 60)
    print("🎯 ANALIZADOR DE OPORTUNIDADES - RosarioGarage")
    print("=" * 60)
    
    # Cargar configuración
    print("\n📂 Cargando configuración...")
    config, brand_aliases, urgency_keywords, agency_keywords = load_config()
    
    if args.debug:
        print(f"   Config: min_price={config.min_price_usd}, max_price={config.max_price_usd}")
        print(f"   Brands aliases: {len(brand_aliases)}")
        print(f"   Urgency keywords: {len(urgency_keywords)}")
    
    # Cargar datos
    print(f"\n📊 Cargando datos desde {args.db}...")
    raw_vehicles, price_history, dolar_mep = load_data_from_db(args.db)
    print(f"   Vehículos cargados: {len(raw_vehicles)}")
    print(f"   Historial de precios: {len(price_history)} vehículos con cambios")
    print(f"   Dólar MEP: ${dolar_mep:,.2f}")
    
    # Normalizar datos
    print("\n🔧 Normalizando datos...")
    normalizer = DataNormalizer(brand_aliases, config)
    
    vehicles: List[Vehicle] = []
    skipped = 0
    
    for raw in raw_vehicles:
        v = Vehicle(
            id=raw['id'],
            url=raw['url'],
            marca=raw['marca'] or '',
            modelo=raw['modelo'] or '',
            version=raw.get('version'),
            año=raw['año'],
            kilometros=raw['kilometros'] or 0,
            precio_usd=raw['precio_usd'] or 0,
            precio_ars=raw['precio_ars'] or 0,
            agencia=raw.get('agencia'),
            dias_publicado=raw['dias_publicado'] or 0,
            primera_vista=raw['primera_vista'],
            ultima_vista=raw['ultima_vista']
        )
        
        # Normalizar
        v.marca_normalizada = normalizer.normalize_brand(v.marca)
        v.modelo_normalizado = normalizer.normalize_model(v.modelo, v.marca_normalizada)
        v.es_agencia = normalizer.detect_agency(v.agencia, agency_keywords)
        
        # Validar
        if normalizer.is_valid_vehicle(v):
            vehicles.append(v)
        else:
            skipped += 1
    
    print(f"   Vehículos válidos: {len(vehicles)}")
    print(f"   Descartados: {skipped}")
    
    # Calcular estadísticas de mercado
    print("\n📈 Calculando estadísticas de mercado...")
    analyzer = MarketAnalyzer(config)
    model_stats = analyzer.calculate_model_stats(vehicles)
    
    # Contar por cuadrante
    cuadrantes = defaultdict(int)
    for stats in model_stats.values():
        cuadrantes[stats.cuadrante] += 1
    
    print(f"   Modelos analizados: {len(model_stats)}")
    print(f"   🥇 Oro: {cuadrantes['oro']}")
    print(f"   💎 Clavos rentables: {cuadrantes['clavo_rentable']}")
    print(f"   📦 Commodity: {cuadrantes['commodity']}")
    print(f"   ⚠️ Evitar: {cuadrantes['evitar']}")
    
    # Detectar oportunidades
    print("\n🎯 Detectando oportunidades...")
    detector = OpportunityDetector(config, urgency_keywords)
    opportunities = detector.find_anomalies(vehicles, model_stats, price_history)
    
    high_score = len([o for o in opportunities if o.score >= 60])
    medium_score = len([o for o in opportunities if 40 <= o.score < 60])
    
    print(f"   Total oportunidades: {len(opportunities)}")
    print(f"   🔥 Score ≥60: {high_score}")
    print(f"   ⚡ Score 40-59: {medium_score}")
    
    # Generar reportes
    if args.report or args.csv:
        print("\n📝 Generando reportes...")
        reporter = ReportGenerator()
        
        if args.report:
            market_path = reporter.generate_market_analysis(model_stats, dolar_mep)
            print(f"   ✅ {market_path}")
            
            opp_path = reporter.generate_opportunities_report(opportunities, dolar_mep)
            print(f"   ✅ {opp_path}")
        
        if args.csv:
            csv_path = reporter.generate_models_csv(model_stats)
            print(f"   ✅ {csv_path}")
    
    # Mostrar resumen en consola
    print("\n" + "=" * 60)
    print("📊 RESUMEN")
    print("=" * 60)
    
    print("\n🥇 TOP 5 MODELOS ORO:")
    oro_models = sorted(
        [s for s in model_stats.values() if s.cuadrante == "oro"],
        key=lambda x: x.spread_percent,
        reverse=True
    )[:5]
    
    for s in oro_models:
        print(f"   {s.marca} {s.modelo} ({s.año_min}-{s.año_max}): "
              f"Spread {s.spread_percent:.1f}%, {s.count} unidades")
    
    print("\n🔥 TOP 5 OPORTUNIDADES:")
    for opp in opportunities[:5]:
        v = opp.vehicle
        print(f"   Score {opp.score:.0f}: {v.marca} {v.modelo} {v.año or ''} "
              f"- ${v.precio_usd:,.0f} ({opp.descuento_vs_q1:.0f}% bajo Q1)")
    
    print("\n" + "=" * 60)
    print("✅ Análisis completado")
    print("=" * 60)
    
    if not (args.report or args.csv):
        print("\n💡 Tip: Usa --report para generar reportes en Markdown")
        print("        Usa --csv para generar lista de modelos a investigar")


if __name__ == "__main__":
    main()
