"""
🔧 Módulo de Normalización de Vehículos v3.0
============================================

Normaliza marca, modelo y versión comparando contra catálogo CSV.

ESTRATEGIA DE BÚSQUEDA (en orden de prioridad):
1. Match exacto en catálogo
2. Match de nombre base (sin números/códigos)
3. Mapeo de códigos (C200→Clase C, 320i→Serie 3, A4→A4)
4. Fuzzy matching (restrictivo, último recurso)
5. Fallback a datos originales

REGLAS:
- Marca: catálogo > alias > raw (nunca vacía)
- Modelo: catálogo > código mapeado > raw (nunca vacío si hay marca)
- Versión: catálogo > NULL (siempre opcional)

Autor: Sistema de Normalización
Versión: 3.0
"""

import csv
import re
import os
import json
import logging
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field

# ═══════════════════════════════════════════════════════════════
# 📦 DEPENDENCIAS OPCIONALES
# ═══════════════════════════════════════════════════════════════

try:
    from unidecode import unidecode
    HAS_UNIDECODE = True
except ImportError:
    HAS_UNIDECODE = False
    def unidecode(text: str) -> str:
        """Fallback para quitar acentos sin unidecode."""
        replacements = {
            'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
            'ä': 'a', 'ë': 'e', 'ï': 'i', 'ö': 'o', 'ü': 'u',
            'ñ': 'n', 'ç': 'c', 'Á': 'A', 'É': 'E', 'Í': 'I',
            'Ó': 'O', 'Ú': 'U', 'Ñ': 'N', 'Ü': 'U',
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

try:
    from rapidfuzz import fuzz, process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 🔧 CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

@dataclass
class NormalizerConfig:
    """Configuración del normalizador."""
    # Thresholds para fuzzy matching (más alto = más estricto)
    fuzzy_threshold_marca: int = 85
    fuzzy_threshold_modelo: int = 88
    fuzzy_threshold_version: int = 80
    
    # Longitud mínima de palabra para fuzzy
    fuzzy_min_word_length: int = 4
    
    # Habilitar/deshabilitar fuzzy
    enable_fuzzy: bool = True
    
    # Rutas de archivos
    catalog_path: str = "config/modelos_y_versiones.csv"
    aliases_path: str = "config/brand_aliases.json"


CONFIG = NormalizerConfig()


# ═══════════════════════════════════════════════════════════════
# 📊 ESTADÍSTICAS
# ═══════════════════════════════════════════════════════════════

@dataclass
class NormalizationStats:
    """Estadísticas de normalización."""
    total: int = 0
    full_match: int = 0         # marca + modelo + versión del catálogo
    partial_match: int = 0      # marca + modelo del catálogo
    marca_only: int = 0         # solo marca del catálogo
    fallback_used: int = 0      # usó datos originales
    code_mapping_used: int = 0  # usó mapeo de códigos
    
    # Detalle por método
    by_method: Dict = field(default_factory=lambda: {
        'exact': 0,
        'base_name': 0,
        'code_mapping': 0,
        'fuzzy': 0,
        'fallback': 0
    })

    def summary(self) -> str:
        if self.total == 0:
            return "Sin datos procesados"
        
        pct = lambda x: f"{(x/self.total*100):.1f}%"
        return (
            f"Total: {self.total} | "
            f"Full: {self.full_match} ({pct(self.full_match)}) | "
            f"Parcial: {self.partial_match} ({pct(self.partial_match)}) | "
            f"Marca: {self.marca_only} ({pct(self.marca_only)}) | "
            f"Fallback: {self.fallback_used} ({pct(self.fallback_used)}) | "
            f"Códigos: {self.code_mapping_used}"
        )


NORM_STATS = NormalizationStats()


# ═══════════════════════════════════════════════════════════════
# 🏷️ ALIASES DE MARCAS
# ═══════════════════════════════════════════════════════════════

DEFAULT_BRAND_ALIASES = {
    # Volkswagen
    'vw': 'volkswagen',
    'volks': 'volkswagen',
    'volkswagon': 'volkswagen',
    
    # Mercedes-Benz
    'mercedes': 'mercedes-benz',
    'mercedes benz': 'mercedes-benz',
    'mercedesbenz': 'mercedes-benz',
    'mb': 'mercedes-benz',
    'benz': 'mercedes-benz',
    
    # BMW
    'bmw': 'bmw',
    
    # Chevrolet
    'chevy': 'chevrolet',
    'gm': 'chevrolet',
    
    # Otras
    'alfa': 'alfa romeo',
    'land rover': 'land rover',
    'landrover': 'land rover',
    'vw': 'volkswagen',
}


# ═══════════════════════════════════════════════════════════════
# 🔢 MAPEO DE CÓDIGOS DE MODELO
# ═══════════════════════════════════════════════════════════════

"""
Este diccionario mapea prefijos de códigos a modelos del catálogo.
Estructura: marca -> { prefijo: modelo_en_catalogo }

Ejemplo: "C200" tiene prefijo "c", que mapea a "clase c"
"""

MODEL_CODE_TO_CATALOG = {
    'mercedes-benz': {
        # Clases principales (letra simple + número)
        'a': 'clase a',      # A180, A200, A250, A35, A45
        'b': 'clase b',      # B180, B200, B250
        'c': 'clase c',      # C180, C200, C220, C250, C300, C43, C63
        'e': 'clase e',      # E200, E220, E300, E350, E400, E53, E63
        's': 'clase s',      # S350, S400, S450, S500, S560, S63, S65
        'g': 'clase g',      # G350, G500, G63
        
        # Series CL
        'cl': 'cl',          # CL500, CL600, CL63
        'cla': 'clase cla',  # CLA180, CLA200, CLA250, CLA35, CLA45
        'clc': 'clase clc',  # CLC180, CLC200, CLC350
        'clk': 'clase clk',  # CLK200, CLK320, CLK500, CLK55
        'cls': 'cls',        # CLS350, CLS400, CLS450, CLS53, CLS63
        
        # Series GL/GLA/GLB/GLC/GLE/GLK
        'gl': 'gl',          # GL350, GL450, GL500, GL63
        'gla': 'clase gla',  # GLA180, GLA200, GLA250, GLA35, GLA45
        'glb': 'clase glb',  # GLB180, GLB200, GLB250, GLB35
        'glc': 'clase glc',  # GLC200, GLC300, GLC43, GLC63
        'gle': 'clase gle',  # GLE300, GLE350, GLE400, GLE450, GLE53, GLE63
        'glk': 'clase glk',  # GLK200, GLK220, GLK250, GLK300, GLK350
        'gls': 'gls',        # GLS350, GLS450, GLS500, GLS63
        
        # Series ML
        'ml': 'clase ml',    # ML250, ML300, ML350, ML400, ML500, ML63
        
        # Series SL/SLC/SLK
        'sl': 'sl',          # SL350, SL400, SL500, SL63, SL65
        'slc': 'slc',        # SLC180, SLC200, SLC300, SLC43
        'slk': 'slk',        # SLK200, SLK250, SLK350, SLK55
        'sls': 'sls',        # SLS AMG
        
        # AMG GT
        'gt': 'gt',          # GT, GT S, GT C, GT R
        'gtr': 'gtr',
        'gts': 'gts',
        'amg gt': 'amg gt',
    },
    
    'bmw': {
        # Series numéricas
        '1': 'serie 1',      # 116i, 118i, 120i, 125i, 130i, 135i, M135i, M140i
        '2': 'serie 2',      # 218i, 220i, 225i, 228i, 230i, M235i, M240i
        '3': 'serie 3',      # 316i, 318i, 320i, 325i, 328i, 330i, 335i, 340i, M3
        '4': 'serie 4',      # 418i, 420i, 428i, 430i, 435i, 440i, M4
        '5': 'serie 5',      # 520i, 525i, 528i, 530i, 535i, 540i, 545i, 550i, M5
        '6': 'serie 6',      # 630i, 640i, 645i, 650i, M6
        '7': 'serie 7',      # 730i, 740i, 745i, 750i, 760i
        '8': 'serie 8',      # 840i, 850i, M8
        
        # Series X
        'x1': 'x1',
        'x2': 'x2',
        'x3': 'x3',
        'x4': 'x4',
        'x5': 'x5',
        'x6': 'x6',
        'x7': 'x7',
        
        # Series Z
        'z3': 'z3',
        'z4': 'z4',
        
        # M
        'm2': 'm2',
        'm3': 'm3',
        'm4': 'm4',
        'm5': 'm5',
        'm6': 'm6',
        'm8': 'm8',
        
        # i
        'i3': 'i3',
        'i4': 'i4',
        'i5': 'i5',
        'i7': 'i7',
        'i8': 'i8',
        'ix': 'ix',
        'ix3': 'ix3',
    },
    
    'audi': {
        # Series A
        'a1': 'a1',
        'a2': 'a2',
        'a3': 'a3',
        'a4': 'a4',
        'a5': 'a5',
        'a6': 'a6',
        'a7': 'a7',
        'a8': 'a8',
        
        # Series Q
        'q2': 'q2',
        'q3': 'q3',
        'q4': 'q4',
        'q5': 'q5',
        'q7': 'q7',
        'q8': 'q8',
        
        # Series S
        's1': 's1',
        's3': 's3',
        's4': 's4',
        's5': 's5',
        's6': 's6',
        's7': 's7',
        's8': 's8',
        
        # Series RS
        'rs3': 'rs3',
        'rs4': 'rs4',
        'rs5': 'rs5',
        'rs6': 'rs6',
        'rs7': 'rs7',
        
        # TT
        'tt': 'tt',
        'tts': 'tts',
        'ttrs': 'tt rs',
        
        # e-tron
        'etron': 'e-tron',
        'e-tron': 'e-tron',
    },
}


# ═══════════════════════════════════════════════════════════════
# 🚫 BLACKLIST DE PALABRAS PARA FUZZY
# ═══════════════════════════════════════════════════════════════

FUZZY_BLACKLIST = {
    # Palabras que generan falsos positivos
    'ano', 'año', 'años', 'anio',
    
    # Combustibles
    'nafta', 'naftero', 'diesel', 'gasoil', 'gnc', 'gas', 'hibrido', 'electrico',
    
    # Transmisión
    'manual', 'automatico', 'automatica', 'secuencial', 'cvt', 'dsg',
    
    # Estados/Condiciones
    'muy', 'buen', 'bueno', 'buena', 'mal', 'estado', 'impecable', 'excelente',
    'perfecto', 'perfecta', 'impecable', 'inmaculado', 'nuevo', 'nueva', 'usado',
    
    # Propiedad
    'unico', 'unica', 'dueno', 'duena', 'dueño', 'dueña', 'titular', 'particular',
    
    # Extras comunes
    'full', 'tope', 'gama', 'equipo', 'equipado', 'equipada', 'completo', 'completa',
    'cuero', 'techo', 'solar', 'llantas', 'alarma', 'sensor', 'sensores', 'camara',
    
    # Transacciones
    'permuto', 'permuta', 'financio', 'financiacion', 'acepto', 'vendo', 'venta',
    'contado', 'efectivo', 'transferencia',
    
    # Vendedor
    'agencia', 'concesionaria', 'concesionario', 'particular', 'dueño',
    
    # Palabras genéricas
    'auto', 'autos', 'coche', 'coches', 'vehiculo', 'vehiculos', 'carro', 'carros',
    'motor', 'caja', 'cambios', 'velocidades',
    
    # Documentación
    'vtv', 'papeles', 'dia', 'patente', 'patentado', 'seguro', 'service',
    
    # Otros
    'consultar', 'precio', 'oferta', 'ofertas', 'urgente', 'oportunidad',
    'original', 'originales', 'funcionando', 'anda', 'funciona',
    'kilometros', 'kms', 'klm', 'litros', 'puertas', 'pts',
    'modelo', 'version', 'serie', 'linea', 'tipo',
}


# ═══════════════════════════════════════════════════════════════
# 🔥 KEYWORDS DE URGENCIA
# ═══════════════════════════════════════════════════════════════

URGENCY_KEYWORDS = {
    'urgente': 30, 'urge': 30, 'urgencia': 30,
    'viajo': 25, 'viaje': 25, 'me voy': 25, 'mudanza': 25,
    'oportunidad': 20, 'negociable': 15, 'escucho': 15,
    'escucho ofertas': 20, 'acepto oferta': 20, 'ofertas': 15,
    'vendo ya': 20, 'venta rapida': 20, 'liquido': 20,
    'financio': 10, 'permuto': 10, 'contado': 10,
    'rebajado': 15, 'rebaja': 15,
    'precio final': -20, 'no negociable': -25, 'firme': -15,
}


# ═══════════════════════════════════════════════════════════════
# 🛠️ FUNCIONES AUXILIARES
# ═══════════════════════════════════════════════════════════════

def normalize_text(text: str) -> str:
    """
    Normaliza texto: minúsculas, sin acentos, sin caracteres especiales.
    Mantiene guiones para marcas como "mercedes-benz".
    """
    if not text:
        return ""
    
    text = str(text).lower().strip()
    text = unidecode(text)
    # Mantener solo letras, números, espacios y guiones
    text = re.sub(r'[^\w\s\-]', ' ', text)
    # Normalizar espacios múltiples
    text = ' '.join(text.split())
    return text


def normalize_key(text: str) -> str:
    """
    Normaliza texto para usar como clave de diccionario.
    Similar a normalize_text pero más agresivo.
    """
    if not text:
        return ""
    
    text = normalize_text(text)
    # Reemplazar guiones por espacios para comparaciones
    # pero mantenerlos en el resultado
    return text


def find_exact_in_text(needle: str, haystack: str) -> bool:
    """
    Busca una coincidencia exacta de palabra completa.
    """
    if not needle or not haystack:
        return False
    
    # Escapar caracteres especiales de regex
    pattern = r'\b' + re.escape(needle) + r'\b'
    return bool(re.search(pattern, haystack, re.IGNORECASE))


def fuzzy_match(
    needle: str, 
    candidates: List[str], 
    threshold: int = 85
) -> Optional[Tuple[str, int]]:
    """
    Búsqueda fuzzy con validaciones.
    Retorna (mejor_match, score) o None.
    """
    if not HAS_RAPIDFUZZ:
        return None
    
    if not needle or not candidates:
        return None
    
    # Validar longitud mínima
    if len(needle) < CONFIG.fuzzy_min_word_length:
        return None
    
    # Validar que no esté en blacklist
    if needle.lower() in FUZZY_BLACKLIST:
        return None
    
    try:
        result = process.extractOne(
            needle,
            candidates,
            scorer=fuzz.ratio,
            score_cutoff=threshold
        )
        return (result[0], result[1]) if result else None
    except Exception as e:
        logger.debug(f"Error en fuzzy_match: {e}")
        return None


def extract_code_prefix(text: str) -> List[Tuple[str, str]]:
    """
    Extrae códigos de modelo del texto.
    
    Ejemplos:
        "C200" -> [("c", "200")]
        "GLA 250" -> [("gla", "250")]
        "ML350 4Matic" -> [("ml", "350")]
        "320i Sport" -> [("3", "20i")]
    
    Returns:
        Lista de tuplas (prefijo, sufijo)
    """
    if not text:
        return []
    
    text_clean = normalize_text(text)
    results = []
    
    # Patrones ordenados de más específico a menos específico
    patterns = [
        # GLA200, GLC300, GLE350, GLK250, etc.
        r'\b(gla|glb|glc|gle|glk|gls)[\s\-]?(\d{2,3})\b',
        
        # CLA200, CLC180, CLK320, CLS350, etc.
        r'\b(cla|clc|clk|cls)[\s\-]?(\d{2,3})\b',
        
        # SL500, SLC200, SLK350, SLS, etc.
        r'\b(sls|slc|slk|sl)[\s\-]?(\d{2,3})?\b',
        
        # ML350, GL500, etc.
        r'\b(ml|gl)[\s\-]?(\d{2,3})\b',
        
        # AMG GT, GTR, GTS
        r'\b(amg\s*gt|gtr|gts|gt)\b',
        
        # A200, B180, C200, E350, S500 (letras simples + número)
        r'\b([abcegs])[\s\-]?(\d{2,3})\b',
        
        # BMW: 320i, 520d, 118i, X3, X5, M3, etc.
        r'\b(x[1-7]|m[2-8]|i[3-8]|z[34])\b',
        r'\b([1-8])[\s\-]?((?:\d{2})[dix]?)\b',
        
        # Audi: A4, Q5, S3, RS6, TT, e-tron
        r'\b(rs[3-7]|a[1-8]|q[2-8]|s[1-8]|tt[s]?|e[\-]?tron)\b',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text_clean, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                prefix = match[0].lower().replace(' ', '').replace('-', '')
                suffix = match[1].lower() if len(match) > 1 and match[1] else ""
            else:
                prefix = match.lower().replace(' ', '').replace('-', '')
                suffix = ""
            
            if prefix and (prefix, suffix) not in results:
                results.append((prefix, suffix))
    
    return results


# ═══════════════════════════════════════════════════════════════
# 📚 CATÁLOGO DE VEHÍCULOS
# ═══════════════════════════════════════════════════════════════

class VehicleCatalog:
    """
    Catálogo de vehículos cargado desde CSV.
    Estructura: marca -> modelo -> {versiones}
    """
    
    def __init__(self):
        self.catalog: Dict[str, Dict[str, Set[str]]] = {}
        self.brand_aliases: Dict[str, str] = {}
        self._brands_list: List[str] = []
        self._models_by_brand: Dict[str, List[str]] = {}
        self._loaded = False
    
    def load_from_csv(self, csv_path: str) -> bool:
        """Carga el catálogo desde un archivo CSV."""
        if not os.path.exists(csv_path):
            logger.warning(f"⚠️ Archivo de catálogo no encontrado: {csv_path}")
            return False
        
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                
                for row in reader:
                    if len(row) < 2:
                        continue
                    
                    marca = normalize_key(row[0])
                    modelo = normalize_key(row[1])
                    
                    if not marca or not modelo:
                        continue
                    
                    # Inicializar estructuras si es necesario
                    if marca not in self.catalog:
                        self.catalog[marca] = {}
                    
                    if modelo not in self.catalog[marca]:
                        self.catalog[marca][modelo] = set()
                    
                    # Agregar versiones (columnas 2 en adelante)
                    for version in row[2:]:
                        v = normalize_key(version)
                        if v:
                            self.catalog[marca][modelo].add(v)
            
            self._build_search_structures()
            self._loaded = True
            
            total_models = sum(len(models) for models in self.catalog.values())
            total_versions = sum(
                len(versions) 
                for models in self.catalog.values() 
                for versions in models.values()
            )
            
            logger.info(
                f"✅ Catálogo cargado: {len(self.catalog)} marcas, "
                f"{total_models} modelos, {total_versions} versiones"
            )
            return True
            
        except Exception as e:
            logger.error(f"❌ Error cargando catálogo: {e}")
            return False
    
    def load_aliases(self, aliases_path: str = None) -> bool:
        """Carga aliases de marcas desde JSON o usa los predeterminados."""
        # Cargar aliases predeterminados
        for alias, brand in DEFAULT_BRAND_ALIASES.items():
            self.brand_aliases[normalize_key(alias)] = normalize_key(brand)
        
        # Intentar cargar desde archivo
        if aliases_path and os.path.exists(aliases_path):
            try:
                with open(aliases_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                for alias, brand in data.get('brand_aliases', {}).items():
                    self.brand_aliases[normalize_key(alias)] = normalize_key(brand)
                
                logger.info(f"✅ Aliases cargados: {len(self.brand_aliases)}")
                return True
            except Exception as e:
                logger.debug(f"No se pudo cargar aliases desde archivo: {e}")
        
        return len(self.brand_aliases) > 0
    
    def _build_search_structures(self):
        """Construye estructuras optimizadas para búsqueda."""
        # Lista de marcas ordenada por longitud (más larga primero)
        self._brands_list = sorted(
            self.catalog.keys(), 
            key=len, 
            reverse=True
        )
        
        # Modelos por marca, ordenados por longitud
        self._models_by_brand = {
            marca: sorted(modelos.keys(), key=len, reverse=True)
            for marca, modelos in self.catalog.items()
        }
    
    def resolve_brand_alias(self, brand: str) -> str:
        """Resuelve un alias de marca a su nombre canónico."""
        brand_norm = normalize_key(brand)
        
        # Buscar en aliases
        if brand_norm in self.brand_aliases:
            return self.brand_aliases[brand_norm]
        
        # Buscar sin espacios/guiones
        brand_compact = brand_norm.replace(' ', '').replace('-', '')
        if brand_compact in self.brand_aliases:
            return self.brand_aliases[brand_compact]
        
        return brand_norm
    
    def has_brand(self, brand: str) -> bool:
        """Verifica si una marca existe en el catálogo."""
        brand_resolved = self.resolve_brand_alias(brand)
        return brand_resolved in self.catalog
    
    def has_model(self, brand: str, model: str) -> bool:
        """Verifica si un modelo existe para una marca."""
        brand_resolved = self.resolve_brand_alias(brand)
        model_norm = normalize_key(model)
        return model_norm in self.catalog.get(brand_resolved, {})
    
    def get_brands(self) -> List[str]:
        """Retorna lista de marcas ordenada por longitud."""
        return self._brands_list.copy()
    
    def get_models(self, brand: str) -> List[str]:
        """Retorna modelos de una marca."""
        brand_resolved = self.resolve_brand_alias(brand)
        return self._models_by_brand.get(brand_resolved, [])
    
    def get_versions(self, brand: str, model: str) -> List[str]:
        """Retorna versiones de un modelo."""
        brand_resolved = self.resolve_brand_alias(brand)
        model_norm = normalize_key(model)
        versions = self.catalog.get(brand_resolved, {}).get(model_norm, set())
        return sorted(versions, key=len, reverse=True)
    
    def is_loaded(self) -> bool:
        """Verifica si el catálogo está cargado."""
        return self._loaded and len(self.catalog) > 0


# Instancia global del catálogo
CATALOG = VehicleCatalog()


# ═══════════════════════════════════════════════════════════════
# 🎯 NORMALIZADOR PRINCIPAL
# ═══════════════════════════════════════════════════════════════

class VehicleNormalizer:
    """
    Normalizador de vehículos contra catálogo.
    
    Estrategia de búsqueda:
    1. Match exacto en catálogo
    2. Match de nombre base (sin números)
    3. Mapeo de códigos (C200 → Clase C)
    4. Fuzzy matching (restrictivo)
    5. Fallback a datos originales
    """
    
    def __init__(self, catalog: VehicleCatalog = None):
        self.catalog = catalog or CATALOG
    
    def normalize(
        self,
        titulo: str = "",
        descripcion: str = "",
        marca_raw: str = "",
        modelo_raw: str = "",
        version_raw: str = ""
    ) -> Dict:
        """
        Normaliza los datos de un vehículo.
        
        Args:
            titulo: Título del anuncio
            descripcion: Descripción del anuncio
            marca_raw: Marca extraída del scraping
            modelo_raw: Modelo extraído (si lo hay)
            version_raw: Versión/submodelo extraído
        
        Returns:
            Dict con marca, modelo, version, norm_status, match_method
        """
        global NORM_STATS
        NORM_STATS.total += 1
        
        # Normalizar inputs
        titulo_norm = normalize_text(titulo)
        desc_norm = normalize_text(descripcion[:500] if descripcion else "")
        marca_norm = normalize_text(marca_raw)
        modelo_norm = normalize_text(modelo_raw)
        version_norm = normalize_text(version_raw)
        
        # Texto combinado para búsquedas
        # Prioridad: título y version_raw primero (más confiables)
        search_text_priority = f"{titulo_norm} {version_norm} {modelo_norm}"
        search_text_full = f"{search_text_priority} {marca_norm} {desc_norm}"
        
        result = {
            'marca': None,
            'modelo': None,
            'version': None,
            'norm_status': 'pending',
            'from_catalog': False,
            'match_method': None,
            'debug': {}  # Info de depuración
        }
        
        # ═══════════════════════════════════════════════════════
        # PASO 1: ENCONTRAR MARCA
        # ═══════════════════════════════════════════════════════
        marca_result = self._find_marca(marca_norm, search_text_full)
        
        if marca_result:
            result['marca'] = marca_result['value']
            result['from_catalog'] = marca_result['from_catalog']
            result['debug']['marca_method'] = marca_result['method']
        else:
            # Sin marca, no podemos continuar
            NORM_STATS.fallback_used += 1
            result['norm_status'] = 'no_marca'
            return result
        
        # ═══════════════════════════════════════════════════════
        # PASO 2: ENCONTRAR MODELO
        # ═══════════════════════════════════════════════════════
        modelo_result = self._find_modelo(
            marca=result['marca'],
            search_text=search_text_priority,
            version_raw=version_norm,
            modelo_raw=modelo_norm,
            titulo=titulo_norm
        )
        
        if modelo_result:
            result['modelo'] = modelo_result['value']
            result['match_method'] = modelo_result['method']
            result['debug']['modelo_method'] = modelo_result['method']
            
            if modelo_result['from_catalog']:
                result['from_catalog'] = True
                NORM_STATS.by_method[modelo_result['method']] = \
                    NORM_STATS.by_method.get(modelo_result['method'], 0) + 1
                
                if modelo_result['method'] == 'code_mapping':
                    NORM_STATS.code_mapping_used += 1
        
        # ═══════════════════════════════════════════════════════
        # PASO 3: ENCONTRAR VERSIÓN (solo si tenemos marca+modelo del catálogo)
        # ═══════════════════════════════════════════════════════
        if result['from_catalog'] and result['modelo']:
            version_result = self._find_version(
                marca=result['marca'],
                modelo=result['modelo'],
                search_text=search_text_full
            )
            
            if version_result:
                result['version'] = version_result['value']
                result['debug']['version_method'] = version_result['method']
        
        # ═══════════════════════════════════════════════════════
        # DETERMINAR STATUS FINAL
        # ═══════════════════════════════════════════════════════
        if result['from_catalog']:
            if result['version']:
                result['norm_status'] = 'full_match'
                NORM_STATS.full_match += 1
            elif result['modelo']:
                result['norm_status'] = 'partial_match'
                NORM_STATS.partial_match += 1
            else:
                result['norm_status'] = 'marca_only'
                NORM_STATS.marca_only += 1
        else:
            result['norm_status'] = 'fallback'
            NORM_STATS.fallback_used += 1
        
        # Limpiar debug en producción (opcional)
        if not logger.isEnabledFor(logging.DEBUG):
            del result['debug']
        
        return result
    
    def _find_marca(self, marca_raw: str, search_text: str) -> Optional[Dict]:
        """
        Busca la marca en el catálogo.
        
        Returns:
            Dict con 'value', 'from_catalog', 'method' o None
        """
        if not self.catalog.is_loaded():
            if marca_raw:
                return {
                    'value': marca_raw,
                    'from_catalog': False,
                    'method': 'fallback'
                }
            return None
        
        # 1. Resolver alias y verificar directamente
        if marca_raw:
            marca_resolved = self.catalog.resolve_brand_alias(marca_raw)
            if self.catalog.has_brand(marca_resolved):
                return {
                    'value': marca_resolved,
                    'from_catalog': True,
                    'method': 'exact'
                }
        
        # 2. Buscar en el texto (priorizar nombres más largos)
        for brand in self.catalog.get_brands():
            if find_exact_in_text(brand, search_text):
                return {
                    'value': brand,
                    'from_catalog': True,
                    'method': 'text_search'
                }
        
        # 3. Fuzzy matching
        if CONFIG.enable_fuzzy and marca_raw:
            match = fuzzy_match(
                marca_raw,
                self.catalog.get_brands(),
                CONFIG.fuzzy_threshold_marca
            )
            if match:
                return {
                    'value': match[0],
                    'from_catalog': True,
                    'method': 'fuzzy'
                }
        
        # 4. Fallback al valor original
        if marca_raw:
            return {
                'value': marca_raw,
                'from_catalog': False,
                'method': 'fallback'
            }
        
        return None
    
    def _find_modelo(
        self,
        marca: str,
        search_text: str,
        version_raw: str,
        modelo_raw: str,
        titulo: str
    ) -> Optional[Dict]:
        """
        Busca el modelo en el catálogo usando múltiples estrategias.
        
        Orden de prioridad:
        1. Match exacto en catálogo
        2. Match de nombre base (version_raw sin números)
        3. Mapeo de código (C200 → Clase C)
        4. Fuzzy matching
        5. Fallback
        """
        modelos_catalogo = self.catalog.get_models(marca)
        combined_text = f"{version_raw} {modelo_raw} {titulo} {search_text}"
        
        # ═══════════════════════════════════════════════════════
        # ESTRATEGIA 1: Match exacto en catálogo
        # ═══════════════════════════════════════════════════════
        for modelo in modelos_catalogo:
            if find_exact_in_text(modelo, combined_text):
                logger.debug(f"✅ Modelo exacto encontrado: '{modelo}'")
                return {
                    'value': modelo,
                    'from_catalog': True,
                    'method': 'exact'
                }
        
        # ═══════════════════════════════════════════════════════
        # ESTRATEGIA 2: Match de nombre base (sin números)
        # ═══════════════════════════════════════════════════════
        # Extraer palabras que podrían ser el nombre del modelo
        # Ej: "Viano Trend" → buscar "viano"
        words_to_check = []
        for text in [version_raw, modelo_raw, titulo]:
            if text:
                # Extraer palabras sin números
                words = [w for w in text.split() 
                        if len(w) >= 3 and not re.match(r'^\d+$', w)]
                words_to_check.extend(words)
        
        for word in words_to_check:
            word_clean = re.sub(r'\d+', '', word).strip()  # Quitar números
            if word_clean and len(word_clean) >= 3:
                for modelo in modelos_catalogo:
                    if word_clean == modelo or find_exact_in_text(word_clean, modelo):
                        logger.debug(f"✅ Modelo por nombre base: '{word}' → '{modelo}'")
                        return {
                            'value': modelo,
                            'from_catalog': True,
                            'method': 'base_name'
                        }
        
        # ═══════════════════════════════════════════════════════
        # ESTRATEGIA 3: Mapeo de código (C200 → Clase C)
        # ═══════════════════════════════════════════════════════
        marca_key = marca.replace(' ', '-')
        code_mappings = MODEL_CODE_TO_CATALOG.get(marca_key, {})
        
        if code_mappings:
            # Extraer códigos del texto
            codes = extract_code_prefix(combined_text)
            
            for prefix, suffix in codes:
                if prefix in code_mappings:
                    modelo_mapeado = code_mappings[prefix]
                    
                    # Verificar que el modelo mapeado existe en el catálogo
                    if self.catalog.has_model(marca, modelo_mapeado):
                        logger.info(
                            f"✅ Código mapeado: '{prefix}{suffix}' → '{modelo_mapeado}'"
                        )
                        return {
                            'value': modelo_mapeado,
                            'from_catalog': True,
                            'method': 'code_mapping'
                        }
                    else:
                        # El modelo mapeado no está en el catálogo,
                        # pero podría existir con otro nombre
                        for modelo in modelos_catalogo:
                            # Buscar si el modelo contiene el prefijo
                            if prefix in modelo.replace('clase ', ''):
                                logger.info(
                                    f"✅ Código parcial: '{prefix}{suffix}' → '{modelo}'"
                                )
                                return {
                                    'value': modelo,
                                    'from_catalog': True,
                                    'method': 'code_mapping'
                                }
        
        # ═══════════════════════════════════════════════════════
        # ESTRATEGIA 4: Fuzzy matching (restrictivo)
        # ═══════════════════════════════════════════════════════
        if CONFIG.enable_fuzzy and modelos_catalogo:
            # Filtrar palabras válidas para fuzzy
            valid_words = [
                w for w in combined_text.split()
                if (len(w) >= CONFIG.fuzzy_min_word_length 
                    and w.lower() not in FUZZY_BLACKLIST
                    and not re.match(r'^(19|20)\d{2}$', w)  # No años
                    and not re.match(r'^\d+$', w))  # No solo números
            ]
            
            for word in valid_words:
                match = fuzzy_match(
                    word,
                    modelos_catalogo,
                    CONFIG.fuzzy_threshold_modelo
                )
                if match:
                    logger.debug(
                        f"🔍 Fuzzy modelo: '{word}' → '{match[0]}' (score: {match[1]})"
                    )
                    return {
                        'value': match[0],
                        'from_catalog': True,
                        'method': 'fuzzy'
                    }
        
        # ═══════════════════════════════════════════════════════
        # ESTRATEGIA 5: Fallback
        # ═══════════════════════════════════════════════════════
        fallback_modelo = self._extract_modelo_fallback(
            version_raw, modelo_raw, titulo, marca
        )
        
        if fallback_modelo:
            return {
                'value': fallback_modelo,
                'from_catalog': False,
                'method': 'fallback'
            }
        
        return None
    
    def _find_version(
        self,
        marca: str,
        modelo: str,
        search_text: str
    ) -> Optional[Dict]:
        """
        Busca la versión en el catálogo.
        """
        versiones = self.catalog.get_versions(marca, modelo)
        
        if not versiones:
            return None
        
        # 1. Match exacto
        for version in versiones:
            if find_exact_in_text(version, search_text):
                return {
                    'value': version,
                    'from_catalog': True,
                    'method': 'exact'
                }
        
        # 2. Fuzzy matching
        if CONFIG.enable_fuzzy:
            valid_words = [
                w for w in search_text.split()
                if (len(w) >= 3 
                    and w.lower() not in FUZZY_BLACKLIST)
            ]
            
            for word in valid_words:
                match = fuzzy_match(
                    word,
                    versiones,
                    CONFIG.fuzzy_threshold_version
                )
                if match:
                    return {
                        'value': match[0],
                        'from_catalog': True,
                        'method': 'fuzzy'
                    }
        
        return None
    
    def _extract_modelo_fallback(
        self,
        version_raw: str,
        modelo_raw: str,
        titulo: str,
        marca: str
    ) -> Optional[str]:
        """
        Extrae el modelo como fallback cuando no hay match en catálogo.
        """
        # Combinar textos
        combined = f"{version_raw} {modelo_raw} {titulo}"
        
        # Remover la marca del texto
        if marca:
            combined = re.sub(
                r'\b' + re.escape(marca) + r'\b',
                '',
                combined,
                flags=re.IGNORECASE
            )
            # También quitar variantes sin guión
            combined = re.sub(
                r'\b' + re.escape(marca.replace('-', ' ')) + r'\b',
                '',
                combined,
                flags=re.IGNORECASE
            )
        
        # Buscar primera palabra significativa
        words = combined.split()
        for word in words:
            word_clean = word.strip()
            
            # Ignorar palabras muy cortas, años, y blacklist
            if (len(word_clean) >= 2 
                and not re.match(r'^(19|20)\d{2}$', word_clean)
                and word_clean.lower() not in FUZZY_BLACKLIST):
                return word_clean
        
        return None


# Instancia global del normalizador
NORMALIZER = VehicleNormalizer()


# ═══════════════════════════════════════════════════════════════
# 🔥 DETECCIÓN DE URGENCIA
# ═══════════════════════════════════════════════════════════════

def extract_urgency_signals(text: str) -> Dict:
    """
    Detecta señales de urgencia en el texto.
    
    Returns:
        Dict con 'has_urgency', 'score', 'keywords_found'
    """
    if not text:
        return {'has_urgency': False, 'score': 0, 'keywords_found': []}
    
    text_lower = text.lower()
    text_norm = normalize_text(text)
    
    score = 0
    keywords_found = []
    
    for keyword, points in URGENCY_KEYWORDS.items():
        if find_exact_in_text(keyword, text_norm) or keyword in text_lower:
            score += points
            if points > 0:
                keywords_found.append(keyword)
    
    # Limitar score entre 0 y 100
    score = max(0, min(100, score))
    
    return {
        'has_urgency': score >= 15,
        'score': score,
        'keywords_found': keywords_found
    }


# ═══════════════════════════════════════════════════════════════
# 🚀 FUNCIONES PÚBLICAS (API)
# ═══════════════════════════════════════════════════════════════

def init_normalizer(
    catalog_path: str = None,
    aliases_path: str = None
) -> bool:
    """
    Inicializa el normalizador cargando el catálogo.
    
    Args:
        catalog_path: Ruta al archivo CSV del catálogo
        aliases_path: Ruta al archivo JSON de aliases
    
    Returns:
        True si se cargó correctamente
    """
    global CATALOG, NORMALIZER, NORM_STATS
    
    # Resetear estadísticas
    NORM_STATS = NormalizationStats()
    
    # Usar rutas de configuración si no se especifican
    catalog_path = catalog_path or CONFIG.catalog_path
    aliases_path = aliases_path or CONFIG.aliases_path
    
    # Cargar catálogo
    catalog_loaded = CATALOG.load_from_csv(catalog_path)
    
    # Cargar aliases
    CATALOG.load_aliases(aliases_path)
    
    # Reinicializar normalizador con el catálogo cargado
    NORMALIZER = VehicleNormalizer(CATALOG)
    
    return catalog_loaded


def normalize_vehicle(
    titulo: str = "",
    descripcion: str = "",
    marca_raw: str = "",
    modelo_raw: str = "",
    version_raw: str = ""
) -> Dict:
    """
    Normaliza los datos de un vehículo.
    
    Esta es la función principal que debe usarse desde otros módulos.
    
    Args:
        titulo: Título del anuncio
        descripcion: Descripción del anuncio
        marca_raw: Marca extraída del scraping
        modelo_raw: Modelo extraído (si existe)
        version_raw: Versión/submodelo extraído
    
    Returns:
        Dict con:
            - marca: str o None
            - modelo: str o None  
            - version: str o None
            - norm_status: 'full_match'|'partial_match'|'marca_only'|'fallback'|'no_marca'
            - from_catalog: bool
            - match_method: str o None
    """
    return NORMALIZER.normalize(
        titulo=titulo,
        descripcion=descripcion,
        marca_raw=marca_raw,
        modelo_raw=modelo_raw,
        version_raw=version_raw
    )


def get_normalization_stats() -> NormalizationStats:
    """Retorna las estadísticas de normalización."""
    return NORM_STATS


def get_catalog() -> VehicleCatalog:
    """Retorna el catálogo cargado."""
    return CATALOG


# ═══════════════════════════════════════════════════════════════
# 🧪 PRUEBAS
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Configurar logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )
    
    print("=" * 70)
    print("🧪 PRUEBA DEL NORMALIZADOR v3.0")
    print("=" * 70)
    
    # Inicializar
    loaded = init_normalizer("config/modelos_y_versiones.csv")
    print(f"\n📚 Catálogo cargado: {loaded}")
    
    if loaded:
        print(f"   Marcas: {len(CATALOG.get_brands())}")
        print(f"   Modelos Mercedes-Benz: {len(CATALOG.get_models('mercedes-benz'))}")
    
    # Casos de prueba
    test_cases = [
        # ═══════════════════════════════════════════════════════
        # CASO ORIGINAL DEL PROBLEMA
        # ═══════════════════════════════════════════════════════
        {
            "name": "🔴 CASO PROBLEMA: Mercedes C200",
            "titulo": "Mercedes Benz C200 Año 2000 Impecable",
            "marca_raw": "Mercedes Benz",
            "version_raw": "C200",
            "descripcion": "Mercedes Benz C200 Año 2000 Nafta. Muy buen estado.",
            "expected": {"marca": "mercedes-benz", "modelo": "clase c"}
        },
        
        # ═══════════════════════════════════════════════════════
        # OTROS CASOS MERCEDES-BENZ
        # ═══════════════════════════════════════════════════════
        {
            "name": "Mercedes E350 Elegance",
            "titulo": "Mercedes Benz E350 Elegance Plus",
            "marca_raw": "Mercedes-Benz",
            "version_raw": "E350 Elegance Plus",
            "expected": {"marca": "mercedes-benz", "modelo": "clase e", "version": "elegance plus"}
        },
        {
            "name": "Mercedes GLA200",
            "titulo": "Mercedes GLA 200 Urban",
            "marca_raw": "Mercedes Benz",
            "version_raw": "GLA200 Urban",
            "expected": {"marca": "mercedes-benz", "modelo": "clase gla", "version": "urban"}
        },
        {
            "name": "Mercedes Viano (modelo directo)",
            "titulo": "Mercedes Benz Viano Trend",
            "marca_raw": "Mercedes-Benz",
            "version_raw": "Viano Trend",
            "expected": {"marca": "mercedes-benz", "modelo": "viano", "version": "trend"}
        },
        {
            "name": "Mercedes ML350",
            "titulo": "Mercedes Benz ML 350 Sport",
            "marca_raw": "Mercedes Benz",
            "version_raw": "ML350 Sport",
            "expected": {"marca": "mercedes-benz", "modelo": "clase ml", "version": "sport"}
        },
        
        # ═══════════════════════════════════════════════════════
        # BMW
        # ═══════════════════════════════════════════════════════
        {
            "name": "BMW 320i",
            "titulo": "BMW 320i Sport 2019",
            "marca_raw": "BMW",
            "version_raw": "320i Sport",
            "expected": {"marca": "bmw", "modelo": "serie 3"}
        },
        
        # ═══════════════════════════════════════════════════════
        # AUDI
        # ═══════════════════════════════════════════════════════
        {
            "name": "Audi A4",
            "titulo": "Audi A4 2.0 TFSI",
            "marca_raw": "Audi",
            "version_raw": "A4 2.0 TFSI",
            "expected": {"marca": "audi", "modelo": "a4"}
        },
        
        # ═══════════════════════════════════════════════════════
        # VOLKSWAGEN
        # ═══════════════════════════════════════════════════════
        {
            "name": "VW Amarok (con alias)",
            "titulo": "VW Amarok Highline 4x4",
            "marca_raw": "VW",
            "version_raw": "Amarok Highline",
            "expected": {"marca": "volkswagen", "modelo": "amarok"}
        },
    ]
    
    print("\n" + "─" * 70)
    
    passed = 0
    failed = 0
    
    for test in test_cases:
        print(f"\n📋 {test['name']}")
        print(f"   Título: {test.get('titulo', '')}")
        print(f"   Marca raw: {test.get('marca_raw', '')}")
        print(f"   Version raw: {test.get('version_raw', '')}")
        
        result = normalize_vehicle(
            titulo=test.get('titulo', ''),
            descripcion=test.get('descripcion', ''),
            marca_raw=test.get('marca_raw', ''),
            modelo_raw=test.get('modelo_raw', ''),
            version_raw=test.get('version_raw', '')
        )
        
        expected = test.get('expected', {})
        
        # Verificar resultados
        marca_ok = result['marca'] == expected.get('marca')
        modelo_ok = result['modelo'] == expected.get('modelo')
        version_ok = (
            result['version'] == expected.get('version') or 
            'version' not in expected
        )
        
        all_ok = marca_ok and modelo_ok and version_ok
        
        if all_ok:
            status = "✅ PASS"
            passed += 1
        else:
            status = "❌ FAIL"
            failed += 1
        
        print(f"\n   {status}")
        print(f"   Resultado:")
        print(f"      marca: {result['marca']}", 
              "✓" if marca_ok else f"✗ (esperado: {expected.get('marca')})")
        print(f"      modelo: {result['modelo']}", 
              "✓" if modelo_ok else f"✗ (esperado: {expected.get('modelo')})")
        print(f"      version: {result['version']}", 
              "✓" if version_ok else f"✗ (esperado: {expected.get('version')})")
        print(f"      method: {result.get('match_method', 'N/A')}")
        print(f"      status: {result['norm_status']}")
    
    # Resumen
    print("\n" + "═" * 70)
    print(f"📊 RESUMEN: {passed} passed, {failed} failed")
    print(f"📈 Stats: {NORM_STATS.summary()}")
    print("=" * 70)
