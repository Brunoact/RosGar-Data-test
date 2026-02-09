#!/usr/bin/env python3
"""
🎯 OPPORTUNITY ANALYZER v2.0 - Sistema de Detección de Oportunidades
=====================================================================
Analiza la base de datos de vehículos para identificar:
1. Modelos con alto spread (margen de arbitraje)
2. Vehículos específicos subvaluados
3. Señales de vendedores motivados (visitas, días, urgencia, re-publicaciones)

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
    spread_threshold_high: float = 15.0
    volume_threshold_high: int = 10
    anomaly_threshold: float = 15.0
    
    # Scoring weights (total = 1.0)
    w_price_discount: float = 0.25
    w_days_published: float = 0.15
    w_price_drops: float = 0.15
    w_seller_type: float = 0.15
    w_seller_size: float = 0.05
    w_urgency: float = 0.10
    w_expiration: float = 0.05
    w_visits_ratio: float = 0.05
    w_model_liquidity: float = 0.05
    
    # Pricing
    first_offer_discount: float = 0.15
    target_discount: float = 0.08
    max_price_margin: float = 0.05
    costs_percent: float = 0.03


def load_config() -> AnalysisConfig:
    """Carga configuración desde archivos JSON."""
    config = AnalysisConfig()
    
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
            config.w_price_discount = data['scoring'].get('weight_price_discount', 0.25)
            config.w_days_published = data['scoring'].get('weight_days_published', 0.15)
            config.w_price_drops = data['scoring'].get('weight_price_drops', 0.15)
            config.w_seller_type = data['scoring'].get('weight_seller_type', 0.15)
            config.w_urgency = data['scoring'].get('weight_urgency', 0.10)
        
        if 'pricing' in data:
            config.first_offer_discount = data['pricing'].get('first_offer_discount', 0.15)
            config.target_discount = data['pricing'].get('target_price_discount', 0.08)
            config.max_price_margin = data['pricing'].get('max_price_margin', 0.05)
            config.costs_percent = data['pricing'].get('estimated_costs_percent', 0.03)
    
    return config


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
    
    # Vendedor
    es_particular: bool
    avisos_vendedor: int
    whatsapp: Optional[str]
    
    # Ubicación
    ciudad: Optional[str]
    provincia: Optional[str]
    
    # Señales
    visitas: Optional[int]
    expira_dias: Optional[int]
    tiene_urgencia: bool
    
    # Tracking
    dias_publicado: int
    primera_vista: str
    ultima_vista: str
    
    # Calculados
    km_por_año: float = 0.0
    visitas_por_dia: float = 0.0
    
    def __post_init__(self):
        if self.año and self.año > 0:
            años_uso = max(1, datetime.now().year - self.año)
            self.km_por_año = self.kilometros / años_uso if self.kilometros else 0
        
        if self.visitas and self.dias_publicado > 0:
            self.visitas_por_dia = self.visitas / self.dias_publicado


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
    precio_q1: float = 0
    precio_q3: float = 0
    precio_iqr: float = 0
    
    # Spread (margen de arbitraje teórico)
    spread_percent: float = 0
    
    # Clasificación
    cuadrante: str = ""
    
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
    descuento_vs_q1: float = 0
    descuento_vs_median: float = 0
    
    # Señales detectadas
    precio_drops: int = 0
    es_republication: bool = False
    
    # Scoring
    score: float = 0
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    
    # Precios sugeridos
    precio_primera_oferta: float = 0
    precio_objetivo: float = 0
    precio_maximo: float = 0
    margen_estimado: float = 0
    
    # Evaluación
    factibilidad: str = ""
    notas: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)


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
        """
        if year_ranges is None:
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
            
            for año_min, año_max in year_ranges:
                if año_min <= v.año <= año_max:
                    key = f"{v.marca}|{v.modelo}|{año_min}-{año_max}"
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
            
            # Calcular spread
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
            return "oro"
        elif high_spread and not high_volume:
            return "clavo_rentable"
        elif not high_spread and high_volume:
            return "commodity"
        else:
            return "evitar"


# ═══════════════════════════════════════════════════════════════
# 🎯 DETECCIÓN DE OPORTUNIDADES
# ═══════════════════════════════════════════════════════════════

class OpportunityDetector:
    """Detecta vehículos subvaluados y oportunidades de compra."""
    
    def __init__(self, config: AnalysisConfig):
        self.config = config
    
    def find_opportunities(
        self,
        vehicles: List[Vehicle],
        model_stats: Dict[str, ModelStats],
        price_history: Dict[str, List[Tuple[str, float, float]]],
        republications: Dict[str, int]
    ) -> List[Opportunity]:
        """
        Encuentra vehículos con precios anormalmente bajos y señales de motivación.
        
        Args:
            vehicles: Lista de vehículos
            model_stats: Estadísticas por modelo
            price_history: vehicle_id -> [(fecha, precio, variacion)]
            republications: fingerprint -> días acumulados
        """
        opportunities = []
        
        for v in vehicles:
            # Buscar estadísticas del modelo
            stats = self._find_model_stats(v, model_stats)
            if not stats:
                continue
            
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
            
            # Contar bajadas de precio
            if v.id in price_history:
                drops = [h for h in price_history[v.id] if h[2] and h[2] < 0]
                opp.precio_drops = len(drops)
            
            # Calcular precios sugeridos
            self._calculate_suggested_prices(opp, stats)
            
            # Calcular score mejorado
            opp.score, opp.score_breakdown = self._calculate_score(opp, stats)
            
            # Detectar señales
            opp.signals = self._detect_signals(opp)
            
            # Evaluar factibilidad
            opp.factibilidad, opp.notas = self._evaluate_feasibility(opp)
            
            opportunities.append(opp)
        
        # Ordenar por score descendente
        opportunities.sort(key=lambda x: x.score, reverse=True)
        
        return opportunities
    
    def _find_model_stats(
        self,
        vehicle: Vehicle,
        model_stats: Dict[str, ModelStats]
    ) -> Optional[ModelStats]:
        """Busca las estadísticas del modelo para un vehículo."""
        for key, stats in model_stats.items():
            if (stats.marca == vehicle.marca and
                stats.modelo == vehicle.modelo and
                vehicle.año and
                stats.año_min <= vehicle.año <= stats.año_max):
                return stats
        return None
    
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
        margen_minimo = self.config.max_price_margin + self.config.costs_percent
        opp.precio_maximo = round(
            stats.precio_median * (1 - margen_minimo),
            -2
        )
        
        # Asegurar que precio máximo no supere el actual
        if opp.precio_maximo > precio_actual:
            opp.precio_maximo = precio_actual
        
        # Calcular margen estimado
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
        Usa todos los datos disponibles para mejor precisión.
        """
        breakdown = {}
        v = opp.vehicle
        
        # ═══════════════════════════════════════════════════════
        # 1. DESCUENTO VS PRECIO DE MERCADO (25%)
        # ═══════════════════════════════════════════════════════
        # Más descuento = mejor
        score_descuento = min(100, opp.descuento_vs_q1 * 3)  # 33% descuento = 100 pts
        breakdown['descuento'] = round(score_descuento * self.config.w_price_discount, 2)
        
        # ═══════════════════════════════════════════════════════
        # 2. DÍAS PUBLICADO (15%)
        # ═══════════════════════════════════════════════════════
        # Más días = más negociable
        dias = v.dias_publicado
        if dias >= 45:
            score_dias = 100  # Muy motivado
        elif dias >= 30:
            score_dias = 80
        elif dias >= 14:
            score_dias = 50
        elif dias >= 7:
            score_dias = 30
        else:
            score_dias = 10  # Recién publicado
        breakdown['dias_publicado'] = round(score_dias * self.config.w_days_published, 2)
        
        # ═══════════════════════════════════════════════════════
        # 3. BAJADAS DE PRECIO (15%)
        # ═══════════════════════════════════════════════════════
        score_bajadas = min(100, opp.precio_drops * 35)  # 3+ bajadas = 100 pts
        breakdown['bajadas_precio'] = round(score_bajadas * self.config.w_price_drops, 2)
        
        # ═══════════════════════════════════════════════════════
        # 4. TIPO DE VENDEDOR (15%)
        # ═══════════════════════════════════════════════════════
        if v.es_particular:
            score_vendedor = 85  # Particulares más negociables
        else:
            score_vendedor = 40  # Agencias menos flexibles
        
        # Bonus por urgencia detectada en descripción
        if v.tiene_urgencia:
            score_vendedor = min(100, score_vendedor + 25)
        
        breakdown['tipo_vendedor'] = round(score_vendedor * self.config.w_seller_type, 2)
        
        # ═══════════════════════════════════════════════════════
        # 5. TAMAÑO DEL VENDEDOR (5%)
        # ═══════════════════════════════════════════════════════
        if v.es_particular:
            score_size = 80  # Particulares = pequeños = más flexibles
        elif v.avisos_vendedor:
            if v.avisos_vendedor <= 5:
                score_size = 70   # Agencia pequeña, más flexible
            elif v.avisos_vendedor <= 20:
                score_size = 50   # Agencia mediana
            elif v.avisos_vendedor <= 50:
                score_size = 30   # Agencia grande
            else:
                score_size = 15   # Mega agencia, precio fijo
        else:
            score_size = 50
        breakdown['tamaño_vendedor'] = round(score_size * self.config.w_seller_size, 2)
        
        # ═══════════════════════════════════════════════════════
        # 6. URGENCIA DETECTADA (10%)
        # ═══════════════════════════════════════════════════════
        if v.tiene_urgencia:
            score_urgencia = 100
        else:
            score_urgencia = 0
        breakdown['urgencia'] = round(score_urgencia * self.config.w_urgency, 2)
        
        # ═══════════════════════════════════════════════════════
        # 7. EXPIRACIÓN (5%)
        # ═══════════════════════════════════════════════════════
        if v.expira_dias is not None:
            if v.expira_dias <= 3:
                score_expira = 100  # Extremadamente urgente
            elif v.expira_dias <= 7:
                score_expira = 80   # Muy urgente
            elif v.expira_dias <= 14:
                score_expira = 50
            elif v.expira_dias <= 21:
                score_expira = 30
            else:
                score_expira = 10
        else:
            score_expira = 30  # Desconocido
        breakdown['expiracion'] = round(score_expira * self.config.w_expiration, 2)
        
        # ═══════════════════════════════════════════════════════
        # 8. RATIO VISITAS/DÍAS (5%)
        # ═══════════════════════════════════════════════════════
        # Pocas visitas + muchos días = publicación "escondida" = oportunidad
        # Muchas visitas + no se vendió = posible problema
        if v.visitas is not None and v.dias_publicado > 0:
            visitas_por_dia = v.visitas / v.dias_publicado
            if visitas_por_dia < 1:
                score_visitas = 90   # Muy pocas visitas, oportunidad oculta
            elif visitas_por_dia < 3:
                score_visitas = 70
            elif visitas_por_dia < 5:
                score_visitas = 50
            elif visitas_por_dia < 10:
                score_visitas = 30
            else:
                score_visitas = 10   # Muchos lo vieron y no compraron
        else:
            score_visitas = 40
        breakdown['visitas_ratio'] = round(score_visitas * self.config.w_visits_ratio, 2)
        
        # ═══════════════════════════════════════════════════════
        # 9. LIQUIDEZ DEL MODELO (5%)
        # ═══════════════════════════════════════════════════════
        if stats.count >= 20:
            score_liquidez = 90
        elif stats.count >= 10:
            score_liquidez = 70
        elif stats.count >= 5:
            score_liquidez = 50
        else:
            score_liquidez = 30
        breakdown['liquidez'] = round(score_liquidez * self.config.w_model_liquidity, 2)
        
        # Total
        total = sum(breakdown.values())
        
        return round(total, 1), breakdown
    
    def _detect_signals(self, opp: Opportunity) -> List[str]:
        """Detecta señales importantes para el reporte."""
        signals = []
        v = opp.vehicle
        
        # Precio
        if opp.descuento_vs_q1 >= 25:
            signals.append(f"💰 {opp.descuento_vs_q1:.0f}% bajo Q1 del mercado")
        elif opp.descuento_vs_q1 >= 15:
            signals.append(f"📉 {opp.descuento_vs_q1:.0f}% bajo Q1")
        
        # Días publicado
        if v.dias_publicado >= 45:
            signals.append(f"⏰ {v.dias_publicado} días publicado - vendedor probablemente frustrado")
        elif v.dias_publicado >= 30:
            signals.append(f"📅 {v.dias_publicado} días publicado")
        
        # Bajadas de precio
        if opp.precio_drops >= 3:
            signals.append(f"📉 Ha bajado el precio {opp.precio_drops} veces")
        elif opp.precio_drops >= 1:
            signals.append(f"📉 {opp.precio_drops} bajada(s) de precio")
        
        # Urgencia
        if v.tiene_urgencia:
            signals.append("🔥 Palabras de urgencia detectadas en descripción")
        
        # Expiración
        if v.expira_dias is not None and v.expira_dias <= 7:
            signals.append(f"⚡ Expira en {v.expira_dias} días")
        
        # Visitas bajas
        if v.visitas is not None and v.dias_publicado > 7:
            visitas_dia = v.visitas / max(1, v.dias_publicado)
            if visitas_dia < 2:
                signals.append(f"👁️ Solo {v.visitas} visitas en {v.dias_publicado} días - oportunidad oculta")
        
        # Particular
        if v.es_particular:
            signals.append("👤 Vendedor particular - más negociable")
        
        # Agencia pequeña
        if not v.es_particular and v.avisos_vendedor and v.avisos_vendedor <= 5:
            signals.append(f"🏪 Agencia pequeña ({v.avisos_vendedor} avisos) - posiblemente flexible")
        
        # Kilometraje
        if v.año and v.kilometros:
            años_uso = max(1, datetime.now().year - v.año)
            km_esperados = años_uso * 15000
            if v.kilometros < km_esperados * 0.6:
                signals.append("🔋 Bajo kilometraje para el año - valor adicional")
        
        return signals
    
    def _evaluate_feasibility(self, opp: Opportunity) -> Tuple[str, List[str]]:
        """Evalúa la factibilidad de negociación."""
        notas = []
        score_factibilidad = 0
        v = opp.vehicle
        
        # Días publicado
        if v.dias_publicado >= 30:
            score_factibilidad += 25
            notas.append("✅ +30 días publicado - alta probabilidad de flexibilidad")
        elif v.dias_publicado >= 14:
            score_factibilidad += 15
        
        # Tipo de vendedor
        if v.es_particular:
            score_factibilidad += 25
            notas.append("✅ Vendedor particular - más margen de negociación")
        else:
            notas.append("⚠️ Agencia - menos flexibilidad en precio")
        
        # Urgencia
        if v.tiene_urgencia:
            score_factibilidad += 30
            notas.append("✅ Señales de urgencia detectadas")
        
        # Bajadas de precio
        if opp.precio_drops >= 2:
            score_factibilidad += 20
            notas.append(f"✅ Ya bajó el precio {opp.precio_drops} veces - dispuesto a negociar")
        
        # Expiración
        if v.expira_dias is not None and v.expira_dias <= 7:
            score_factibilidad += 15
            notas.append(f"✅ Publicación expira pronto ({v.expira_dias} días)")
        
        # Calcular descuento que pedimos
        descuento_primera = (
            (v.precio_usd - opp.precio_primera_oferta) / v.precio_usd
        ) * 100
        
        if descuento_primera > 20:
            notas.append(f"⚠️ Primera oferta pide {descuento_primera:.0f}% descuento - puede ser agresivo")
        
        # Clasificar
        if score_factibilidad >= 60:
            return "alta", notas
        elif score_factibilidad >= 35:
            return "media", notas
        else:
            return "baja", notas
    
    def find_hidden_opportunities(
        self,
        vehicles: List[Vehicle],
        model_stats: Dict[str, ModelStats]
    ) -> List[Dict]:
        """
        Detecta oportunidades que el análisis estándar podría perder.
        Patrones específicos de vendedores muy motivados.
        """
        hidden = []
        
        for v in vehicles:
            reasons = []
            bonus_score = 0
            
            # 1. PUBLICACIÓN HUÉRFANA
            # Muchos días, pocas visitas, particular
            if (v.dias_publicado >= 30 and 
                v.visitas is not None and v.visitas < 50 and 
                v.es_particular):
                reasons.append("📦 Publicación huérfana - vendedor probablemente frustrado")
                bonus_score += 20
            
            # 2. EXPIRACIÓN INMINENTE + PARTICULAR
            if (v.expira_dias is not None and 
                v.expira_dias <= 5 and 
                v.es_particular):
                reasons.append("⏰ Expira en menos de 5 días + particular")
                bonus_score += 25
            
            # 3. COMBINACIÓN MORTAL: urgencia + particular + muchos días
            if (v.tiene_urgencia and 
                v.es_particular and 
                v.dias_publicado >= 21):
                reasons.append("🔥 Urgencia + particular + 3+ semanas publicado")
                bonus_score += 30
            
            # 4. BAJO KILOMETRAJE SUBVALUADO
            if v.año and v.kilometros:
                años_uso = max(1, datetime.now().year - v.año)
                km_esperados = años_uso * 15000
                
                if v.kilometros < km_esperados * 0.5:
                    reasons.append("🔋 Kilometraje muy bajo para el año")
                    bonus_score += 15
            
            # 5. AGENCIA PEQUEÑA DESESPERADA
            if (not v.es_particular and 
                v.avisos_vendedor and v.avisos_vendedor <= 3 and
                v.dias_publicado >= 30):
                reasons.append("🏪 Agencia muy pequeña con publicación antigua")
                bonus_score += 15
            
            if reasons and bonus_score >= 20:
                hidden.append({
                    'vehicle': v,
                    'reasons': reasons,
                    'bonus_score': bonus_score
                })
        
        # Ordenar por bonus_score
        hidden.sort(key=lambda x: x['bonus_score'], reverse=True)
        
        return hidden[:20]  # Top 20


# ═══════════════════════════════════════════════════════════════
# 📋 GENERACIÓN DE REPORTES
# ═══════════════════════════════════════════════════════════════

class ReportGenerator:
    """Genera reportes en diferentes formatos."""
    
    def __init__(self, config: AnalysisConfig, output_dir: str = REPORTS_DIR):
        self.config = config
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

|                    | 📈 ALTO SPREAD (>{self.config.spread_threshold_high}%) | 📉 BAJO SPREAD |
|--------------------|---------------------------|----------------|
| **📦 Alto Volumen (≥{self.config.volume_threshold_high})** | 🥇 ORO ({len(oro)})        | 📦 Commodity ({len(commodity)}) |
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

1. **Modelos ORO**: Prioriza buscar estos modelos. Alto volumen significa que siempre hay oferta
2. **Clavos Rentables**: Ten alertas activas - cuando aparezcan baratos, actúa rápido
3. **Commodity**: Solo compra si el descuento es excepcional (>25% bajo Q1)
4. **Evitar**: No pierdas tiempo con estos modelos

### 🔍 Próximos Pasos

1. Revisa las **Oportunidades TOP** en el reporte OPPORTUNITIES.md
2. Contacta primero a vendedores con **score ≥60**
3. Usa los **precios sugeridos** como guía de negociación
"""
        
        path = os.path.join(self.output_dir, 'MARKET_ANALYSIS.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(report)
        
        return path
    
    def generate_opportunities_report(
        self,
        opportunities: List[Opportunity],
        hidden: List[Dict],
        dolar_mep: float
    ) -> str:
        """Genera reporte de oportunidades específicas."""
        
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
            for i, opp in enumerate(high_score[:15], 1):
                v = opp.vehicle
                s = opp.model_stats
                
                report += f"""### {i}. {v.marca.upper() if v.marca else 'N/A'} {v.modelo.upper() if v.modelo else 'N/A'} {v.año or 'S/A'}

| Dato | Valor |
|------|-------|
| **Score** | **{opp.score:.0f}/100** |
| **Precio publicado** | USD ${v.precio_usd:,.0f} |
| **Q1 del modelo** | USD ${s.precio_q1:,.0f} |
| **Mediana del modelo** | USD ${s.precio_median:,.0f} |
| **Descuento vs Q1** | **{opp.descuento_vs_q1:.1f}%** |
| **Días publicado** | {v.dias_publicado} días |
| **Kilómetros** | {v.kilometros:,} km |
| **Visitas** | {v.visitas or 'N/A'} |
| **Vendedor** | {'Particular' if v.es_particular else f'Agencia ({v.avisos_vendedor} avisos)'} |
| **Factibilidad** | {opp.factibilidad.upper()} |

**💰 Precios Sugeridos:**
- Primera oferta: **USD ${opp.precio_primera_oferta:,.0f}** (agresiva)
- Precio objetivo: **USD ${opp.precio_objetivo:,.0f}** (realista)
- Precio máximo: **USD ${opp.precio_maximo:,.0f}** (límite)
- Margen estimado: **{opp.margen_estimado:.1f}%**

**📊 Desglose del Score:**
"""
                for key, val in opp.score_breakdown.items():
                    report += f"- {key}: {val:.1f}\n"
                
                report += "\n**🚦 Señales detectadas:**\n"
                for signal in opp.signals:
                    report += f"- {signal}\n"
                
                report += f"\n🔗 [Ver publicación]({v.url})\n\n---\n\n"
        else:
            report += "*No se encontraron oportunidades con score ≥60*\n\n"
        
        # Oportunidades medias
        report += f"""
## ⚡ Oportunidades Medias (Score 40-59)

| # | Vehículo | Precio | Desc. vs Q1 | Días | Score | Link |
|---|----------|--------|-------------|------|-------|------|
"""
        for i, opp in enumerate(medium_score[:20], 1):
            v = opp.vehicle
            marca = v.marca.title() if v.marca else 'N/A'
            modelo = v.modelo.title() if v.modelo else ''
            report += f"| {i} | {marca} {modelo} {v.año or ''} | ${v.precio_usd:,.0f} | {opp.descuento_vs_q1:.0f}% | {v.dias_publicado} | {opp.score:.0f} | [Ver]({v.url}) |\n"
        
        # Oportunidades ocultas
        if hidden:
            report += f"""

---

## 🔮 Oportunidades Ocultas

*Patrones especiales de vendedores muy motivados*

"""
            for i, h in enumerate(hidden[:10], 1):
                v = h['vehicle']
                marca = v.marca.title() if v.marca else 'N/A'
                modelo = v.modelo.title() if v.modelo else ''
                report += f"""### {i}. {marca} {modelo} {v.año or ''}
- **Precio:** USD ${v.precio_usd:,.0f}
- **Días publicado:** {v.dias_publicado}
- **Bonus score:** +{h['bonus_score']}

**Razones:**
"""
                for reason in h['reasons']:
                    report += f"- {reason}\n"
                report += f"\n🔗 [Ver publicación]({v.url})\n\n"
        
        report += """
---

## 📌 Cómo Interpretar

- **Score ≥60**: Contactar hoy - alta probabilidad de buen negocio
- **Score 40-59**: Evaluar caso por caso - pueden ser buenos con negociación
- **Oportunidades Ocultas**: Patrones especiales que indican vendedores muy motivados

### ⚠️ Recordatorios

1. **Siempre inspecciona** el vehículo antes de comprar
2. **Verifica documentación** (título, deudas, multas, VTV)
3. Los precios son en USD y pueden variar con el dólar
4. El margen estimado no incluye costos de reparación
"""
        
        path = os.path.join(self.output_dir, 'OPPORTUNITIES.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(report)
        
        return path
    
    def generate_models_csv(self, model_stats: Dict[str, ModelStats]) -> str:
        """Genera CSV con modelos para investigar."""
        
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
        
        lines = [
            "Prioridad,Cuadrante,Marca,Modelo,Años,Unidades,Q1_USD,Mediana_USD,Spread_Pct,Dias_Prom"
        ]
        
        for i, s in enumerate(models[:50], 1):
            line = f'{i},{s.cuadrante},{s.marca},{s.modelo},{s.año_min}-{s.año_max},{s.count},{s.precio_q1:.0f},{s.precio_median:.0f},{s.spread_percent:.1f},{s.dias_promedio_publicado:.0f}'
            lines.append(line)
        
        path = os.path.join(self.output_dir, 'models_to_research.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        
        return path
    
    def generate_opportunities_csv(self, opportunities: List[Opportunity]) -> str:
        """Genera CSV con oportunidades para análisis."""
        
        lines = [
            "Score,Marca,Modelo,Año,Precio_USD,Q1_USD,Descuento_Pct,Dias,Visitas,Particular,Urgencia,Factibilidad,URL"
        ]
        
        for opp in opportunities[:100]:
            v = opp.vehicle
            line = (
                f'{opp.score:.0f},'
                f'{v.marca or ""},'
                f'{v.modelo or ""},'
                f'{v.año or ""},'
                f'{v.precio_usd:.0f},'
                f'{opp.model_stats.precio_q1:.0f},'
                f'{opp.descuento_vs_q1:.1f},'
                f'{v.dias_publicado},'
                f'{v.visitas or ""},'
                f'{"Si" if v.es_particular else "No"},'
                f'{"Si" if v.tiene_urgencia else "No"},'
                f'{opp.factibilidad},'
                f'{v.url}'
            )
            lines.append(line)
        
        path = os.path.join(self.output_dir, 'opportunities.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        
        return path


# ═══════════════════════════════════════════════════════════════
# 🚀 EJECUCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════

def load_data_from_db(db_path: str) -> Tuple[List[Dict], Dict[str, List], Dict[str, int], float]:
    """Carga datos de la base de datos."""
    
    if not os.path.exists(db_path):
        print(f"❌ No se encontró la base de datos: {db_path}")
        sys.exit(1)
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Cargar vehículos activos
    vehicles = []
    for row in conn.execute("""
        SELECT 
            id, url, marca, modelo, version, año, kilometros,
            precio_usd, es_particular, avisos_vendedor, whatsapp,
            ciudad, provincia, visitas, expira_dias, tiene_urgencia,
            dias_publicado, primera_vista, ultima_vista
        FROM vehicles
        WHERE activo = 1
    """):
        vehicles.append(dict(row))
    
    # Cargar historial de precios
    price_history = defaultdict(list)
    for row in conn.execute("""
        SELECT vehicle_id, fecha, precio_usd, variacion_pct
        FROM price_history
        ORDER BY vehicle_id, fecha
    """):
        price_history[row['vehicle_id']].append(
            (row['fecha'], row['precio_usd'], row['variacion_pct'])
        )
    
    # Cargar re-publicaciones
    republications = {}
    for row in conn.execute("""
        SELECT fingerprint, SUM(dias_acumulados) as total_dias
        FROM republication_log
        GROUP BY fingerprint
    """):
        republications[row['fingerprint']] = row['total_dias']
    
    # Cargar dólar
    dolar = conn.execute(
        "SELECT value FROM scrape_metadata WHERE key = 'last_dolar_mep'"
    ).fetchone()
    dolar_mep = float(dolar[0]) if dolar else 1000.0
    
    conn.close()
    
    return vehicles, dict(price_history), republications, dolar_mep


def main():
    """Función principal."""
    
    parser = argparse.ArgumentParser(description='Analizador de Oportunidades de Compra v2.0')
    parser.add_argument('--report', action='store_true', help='Generar reportes markdown')
    parser.add_argument('--csv', action='store_true', help='Generar CSV para investigación')
    parser.add_argument('--debug', action='store_true', help='Modo debug con más información')
    parser.add_argument('--db', default=DB_PATH, help='Ruta a la base de datos')
    args = parser.parse_args()
    
    print("=" * 60)
    print("🎯 ANALIZADOR DE OPORTUNIDADES v2.0 - RosarioGarage")
    print("=" * 60)
    
    # Cargar configuración
    print("\n📂 Cargando configuración...")
    config = load_config()
    
    if args.debug:
        print(f"   Config: min_price={config.min_price_usd}, max_price={config.max_price_usd}")
    
    # Cargar datos
    print(f"\n📊 Cargando datos desde {args.db}...")
    raw_vehicles, price_history, republications, dolar_mep = load_data_from_db(args.db)
    print(f"   Vehículos cargados: {len(raw_vehicles)}")
    print(f"   Historial de precios: {len(price_history)} vehículos con cambios")
    print(f"   Re-publicaciones: {len(republications)} fingerprints")
    print(f"   Dólar MEP: ${dolar_mep:,.2f}")
    
    # Convertir a objetos Vehicle
    print("\n🔧 Procesando datos...")
    vehicles: List[Vehicle] = []
    skipped = 0
    
    for raw in raw_vehicles:
        # Validar datos mínimos
        if not raw['precio_usd'] or raw['precio_usd'] < config.min_price_usd:
            skipped += 1
            continue
        if raw['precio_usd'] > config.max_price_usd:
            skipped += 1
            continue
        if raw['año'] and (raw['año'] < config.min_year or raw['año'] > config.max_year):
            skipped += 1
            continue
        if not raw['marca']:
            skipped += 1
            continue
        
        v = Vehicle(
            id=raw['id'],
            url=raw['url'],
            marca=raw['marca'] or '',
            modelo=raw['modelo'] or '',
            version=raw.get('version'),
            año=raw['año'],
            kilometros=raw['kilometros'] or 0,
            precio_usd=raw['precio_usd'],
            es_particular=bool(raw.get('es_particular', 1)),
            avisos_vendedor=raw.get('avisos_vendedor', 1) or 1,
            whatsapp=raw.get('whatsapp'),
            ciudad=raw.get('ciudad'),
            provincia=raw.get('provincia'),
            visitas=raw.get('visitas'),
            expira_dias=raw.get('expira_dias'),
            tiene_urgencia=bool(raw.get('tiene_urgencia', 0)),
            dias_publicado=raw.get('dias_publicado', 0) or 0,
            primera_vista=raw['primera_vista'],
            ultima_vista=raw['ultima_vista']
        )
        vehicles.append(v)
    
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
    detector = OpportunityDetector(config)
    opportunities = detector.find_opportunities(
        vehicles, model_stats, price_history, republications
    )
    
    # Detectar oportunidades ocultas
    hidden = detector.find_hidden_opportunities(vehicles, model_stats)
    
    high_score = len([o for o in opportunities if o.score >= 60])
    medium_score = len([o for o in opportunities if 40 <= o.score < 60])
    
    print(f"   Total oportunidades: {len(opportunities)}")
    print(f"   🔥 Score ≥60: {high_score}")
    print(f"   ⚡ Score 40-59: {medium_score}")
    print(f"   🔮 Oportunidades ocultas: {len(hidden)}")
    
    # Generar reportes
    if args.report or args.csv:
        print("\n📝 Generando reportes...")
        reporter = ReportGenerator(config)
        
        if args.report:
            market_path = reporter.generate_market_analysis(model_stats, dolar_mep)
            print(f"   ✅ {market_path}")
            
            opp_path = reporter.generate_opportunities_report(opportunities, hidden, dolar_mep)
            print(f"   ✅ {opp_path}")
        
        if args.csv:
            csv_path = reporter.generate_models_csv(model_stats)
            print(f"   ✅ {csv_path}")
            
            opp_csv_path = reporter.generate_opportunities_csv(opportunities)
            print(f"   ✅ {opp_csv_path}")
    
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
        marca = v.marca.title() if v.marca else 'N/A'
        modelo = v.modelo.title() if v.modelo else ''
        print(f"   Score {opp.score:.0f}: {marca} {modelo} {v.año or ''} "
              f"- ${v.precio_usd:,.0f} ({opp.descuento_vs_q1:.0f}% bajo Q1)")
    
    if hidden:
        print("\n🔮 TOP 3 OPORTUNIDADES OCULTAS:")
        for h in hidden[:3]:
            v = h['vehicle']
            marca = v.marca.title() if v.marca else 'N/A'
            modelo = v.modelo.title() if v.modelo else ''
            print(f"   {marca} {modelo} {v.año or ''} - ${v.precio_usd:,.0f}")
            print(f"      Razones: {', '.join(h['reasons'][:2])}")
    
    print("\n" + "=" * 60)
    print("✅ Análisis completado")
    print("=" * 60)
    
    if not (args.report or args.csv):
        print("\n💡 Tip: Usa --report para generar reportes en Markdown")
        print("        Usa --csv para generar lista de modelos y oportunidades")


if __name__ == "__main__":
    main()
