"""
🔧 Módulo de Normalización de Vehículos
Normaliza marca, modelo y versión usando catálogo CSV y fuzzy matching.
Todo el output es en MINÚSCULAS.
"""

import csv
import re
import os
import json
import logging
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field

# Manejo de dependencias opcionales
try:
    from unidecode import unidecode
    HAS_UNIDECODE = True
except ImportError:
    HAS_UNIDECODE = False
    def unidecode(text: str) -> str:
        """Fallback básico si unidecode no está instalado."""
        replacements = {
            'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
            'ä': 'a', 'ë': 'e', 'ï': 'i', 'ö': 'o', 'ü': 'u',
            'ñ': 'n', 'ç': 'c',
            'Á': 'a', 'É': 'e', 'Í': 'i', 'Ó': 'o', 'Ú': 'u',
            'Ñ': 'n',
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
# 📊 ESTADÍSTICAS DE NORMALIZACIÓN
# ═══════════════════════════════════════════════════════════════
@dataclass
class NormalizationStats:
    total: int = 0
    full_match: int = 0       # marca + modelo + versión encontrados
    marca_modelo: int = 0     # marca + modelo encontrados
    marca_only: int = 0       # solo marca
    no_match: int = 0         # nada encontrado
    fuzzy_marca: int = 0      # marcas corregidas por fuzzy
    fuzzy_modelo: int = 0     # modelos corregidos por fuzzy
    fuzzy_version: int = 0    # versiones corregidas por fuzzy

    def summary(self) -> str:
        if self.total == 0:
            return "Sin datos de normalización"
        return (
            f"Total: {self.total} | "
            f"Completo: {self.full_match} ({self.full_match*100//self.total}%) | "
            f"Marca+Modelo: {self.marca_modelo} | "
            f"Solo marca: {self.marca_only} | "
            f"Sin match: {self.no_match} | "
            f"Fuzzy: M:{self.fuzzy_marca} Mo:{self.fuzzy_modelo} V:{self.fuzzy_version}"
        )


NORM_STATS = NormalizationStats()


# ═══════════════════════════════════════════════════════════════
# 🔧 CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════
@dataclass
class NormalizerConfig:
    # Umbrales de fuzzy matching (0-100)
    fuzzy_threshold_marca: int = 85
    fuzzy_threshold_modelo: int = 80
    fuzzy_threshold_version: int = 75
    
    # Activar/desactivar fuzzy
    enable_fuzzy: bool = True
    
    # Paths por defecto
    catalog_path: str = "config/vehicles_catalog.csv"
    aliases_path: str = "config/brand_aliases.json"


CONFIG = NormalizerConfig()


# ═══════════════════════════════════════════════════════════════
# 📚 CATÁLOGO DE VEHÍCULOS
# ═══════════════════════════════════════════════════════════════
class VehicleCatalog:
    """
    Catálogo jerárquico de marcas -> modelos -> versiones.
    Todo almacenado en minúsculas para comparación case-insensitive.
    """
    
    def __init__(self):
        # Estructura: {marca_norm: {modelo_norm: set(versiones_norm)}}
        self.catalog: Dict[str, Dict[str, Set[str]]] = {}
        
        # Aliases de marcas: {alias_norm: marca_norm}
        self.brand_aliases: Dict[str, str] = {}
        
        # Marcas a ignorar
        self.brands_to_skip: Set[str] = set()
        
        # Listas para búsqueda rápida
        self._all_brands: List[str] = []
        self._models_by_brand: Dict[str, List[str]] = {}
        
        # Cache de resultados
        self._cache: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {}
        self._cache_max_size: int = 10000
    
    def load_from_csv(self, csv_path: str) -> bool:
        """
        Carga el catálogo desde un CSV con formato:
        Marca,Modelo,Version1,Version2,Version3,...
        """
        if not os.path.exists(csv_path):
            logger.warning(f"⚠️ Archivo de catálogo no encontrado: {csv_path}")
            return False
        
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None)  # Saltar header si existe
                
                for row in reader:
                    if len(row) < 2:
                        continue
                    
                    marca = self._normalize_key(row[0])
                    modelo = self._normalize_key(row[1])
                    
                    if not marca or not modelo:
                        continue
                    
                    # Inicializar marca si no existe
                    if marca not in self.catalog:
                        self.catalog[marca] = {}
                    
                    # Inicializar modelo si no existe
                    if modelo not in self.catalog[marca]:
                        self.catalog[marca][modelo] = set()
                    
                    # Agregar versiones (columnas 3 en adelante)
                    for version in row[2:]:
                        version_norm = self._normalize_key(version)
                        if version_norm:
                            self.catalog[marca][modelo].add(version_norm)
            
            # Construir listas para búsqueda
            self._build_search_lists()
            
            total_marcas = len(self.catalog)
            total_modelos = sum(len(m) for m in self.catalog.values())
            total_versiones = sum(
                len(v) for m in self.catalog.values() for v in m.values()
            )
            
            logger.info(
                f"✅ Catálogo cargado: {total_marcas} marcas, "
                f"{total_modelos} modelos, {total_versiones} versiones"
            )
            return True
            
        except Exception as e:
            logger.error(f"❌ Error cargando catálogo: {e}")
            return False
    
    def load_aliases(self, aliases_path: str) -> bool:
        """Carga aliases de marcas desde JSON."""
        if not os.path.exists(aliases_path):
            logger.warning(f"⚠️ Archivo de aliases no encontrado: {aliases_path}")
            return False
        
        try:
            with open(aliases_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Cargar aliases
            aliases = data.get('brand_aliases', {})
            for alias, brand in aliases.items():
                alias_norm = self._normalize_key(alias)
                brand_norm = self._normalize_key(brand)
                if alias_norm and brand_norm:
                    self.brand_aliases[alias_norm] = brand_norm
            
            # Cargar marcas a ignorar
            skip = data.get('brands_to_skip', [])
            for brand in skip:
                brand_norm = self._normalize_key(brand)
                if brand_norm:
                    self.brands_to_skip.add(brand_norm)
            
            logger.info(f"✅ Aliases cargados: {len(self.brand_aliases)} aliases")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error cargando aliases: {e}")
            return False
    
    def _normalize_key(self, text: str) -> str:
        """Normaliza texto para usar como clave de búsqueda."""
        if not text:
            return ""
        
        # A minúsculas
        text = text.lower().strip()
        
        # Remover acentos
        text = unidecode(text)
        
        # Normalizar espacios
        text = ' '.join(text.split())
        
        return text
    
    def _build_search_lists(self):
        """Construye listas ordenadas para búsqueda eficiente."""
        # Marcas ordenadas por longitud (desc) para matchear primero las más largas
        self._all_brands = sorted(
            self.catalog.keys(),
            key=lambda x: len(x),
            reverse=True
        )
        
        # Modelos por marca, también ordenados por longitud
        self._models_by_brand = {}
        for marca, modelos in self.catalog.items():
            self._models_by_brand[marca] = sorted(
                modelos.keys(),
                key=lambda x: len(x),
                reverse=True
            )
    
    def resolve_alias(self, brand: str) -> str:
        """Resuelve un alias de marca a la marca canónica."""
        brand_norm = self._normalize_key(brand)
        return self.brand_aliases.get(brand_norm, brand_norm)
    
    def get_brands(self) -> List[str]:
        """Retorna todas las marcas del catálogo."""
        return self._all_brands.copy()
    
    def get_models(self, brand: str) -> List[str]:
        """Retorna todos los modelos de una marca."""
        brand_norm = self._normalize_key(brand)
        brand_resolved = self.resolve_alias(brand_norm)
        return self._models_by_brand.get(brand_resolved, [])
    
    def get_versions(self, brand: str, model: str) -> Set[str]:
        """Retorna todas las versiones de un modelo."""
        brand_norm = self.resolve_alias(self._normalize_key(brand))
        model_norm = self._normalize_key(model)
        return self.catalog.get(brand_norm, {}).get(model_norm, set())
    
    def brand_exists(self, brand: str) -> bool:
        """Verifica si una marca existe en el catálogo."""
        brand_norm = self.resolve_alias(self._normalize_key(brand))
        return brand_norm in self.catalog
    
    def model_exists(self, brand: str, model: str) -> bool:
        """Verifica si un modelo existe para una marca."""
        brand_norm = self.resolve_alias(self._normalize_key(brand))
        model_norm = self._normalize_key(model)
        return model_norm in self.catalog.get(brand_norm, {})
    
    def version_exists(self, brand: str, model: str, version: str) -> bool:
        """Verifica si una versión existe para un modelo."""
        brand_norm = self.resolve_alias(self._normalize_key(brand))
        model_norm = self._normalize_key(model)
        version_norm = self._normalize_key(version)
        versions = self.catalog.get(brand_norm, {}).get(model_norm, set())
        return version_norm in versions


# Instancia global del catálogo
CATALOG = VehicleCatalog()


# ═══════════════════════════════════════════════════════════════
# 🔍 FUNCIONES DE BÚSQUEDA
# ═══════════════════════════════════════════════════════════════
def normalize_text(text: str) -> str:
    """
    Normaliza texto para comparación.
    - Minúsculas
    - Sin acentos
    - Sin caracteres especiales (excepto espacios)
    - Espacios normalizados
    """
    if not text:
        return ""
    
    text = text.lower().strip()
    text = unidecode(text)
    
    # Reemplazar caracteres especiales por espacio (excepto letras, números, espacios)
    text = re.sub(r'[^\w\s]', ' ', text)
    
    # Normalizar espacios múltiples
    text = ' '.join(text.split())
    
    return text


def find_in_text(needle: str, haystack: str) -> bool:
    """
    Busca una cadena como palabra completa en un texto.
    Evita matches parciales (ej: "128" no debe matchear en "12850").
    """
    if not needle or not haystack:
        return False
    
    # Escapar caracteres especiales de regex
    pattern = r'\b' + re.escape(needle) + r'\b'
    return bool(re.search(pattern, haystack))


def fuzzy_find_best(
    needle: str,
    candidates: List[str],
    threshold: int = 80
) -> Optional[Tuple[str, int]]:
    """
    Encuentra el mejor match fuzzy de un término en una lista de candidatos.
    Retorna (match, score) o None si no hay match sobre el threshold.
    """
    if not HAS_RAPIDFUZZ or not needle or not candidates:
        return None
    
    try:
        result = process.extractOne(
            needle,
            candidates,
            scorer=fuzz.ratio,
            score_cutoff=threshold
        )
        
        if result:
            return (result[0], result[1])
        return None
        
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 🎯 NORMALIZADOR PRINCIPAL
# ═══════════════════════════════════════════════════════════════
class VehicleNormalizer:
    """
    Normaliza marca, modelo y versión de vehículos
    usando el catálogo y fuzzy matching.
    """
    
    def __init__(self, catalog: VehicleCatalog = None):
        self.catalog = catalog or CATALOG
        self._cache: Dict[str, Dict] = {}
    
    def normalize(
        self,
        titulo: str = "",
        descripcion: str = "",
        marca_raw: str = "",
        modelo_raw: str = "",
        version_raw: str = ""
    ) -> Dict:
        """
        Normaliza un vehículo usando todos los datos disponibles.
        
        Args:
            titulo: Título de la publicación
            descripcion: Descripción completa
            marca_raw: Marca extraída del scraping
            modelo_raw: Modelo extraído del scraping
            version_raw: Versión extraída del scraping
        
        Returns:
            Dict con marca, modelo, version normalizados y metadatos
        """
        global NORM_STATS
        NORM_STATS.total += 1
        
        # Construir texto completo para búsqueda
        full_text = normalize_text(
            f"{titulo} {marca_raw} {modelo_raw} {version_raw} {descripcion}"
        )
        
        # Cache key
        cache_key = full_text[:500]  # Limitar tamaño de key
        if cache_key in self._cache:
            return self._cache[cache_key].copy()
        
        result = {
            'marca': None,
            'modelo': None,
            'version': None,
            'marca_original': marca_raw,
            'modelo_original': modelo_raw,
            'version_original': version_raw,
            'normalizacion': 'sin_match',
            'fuzzy_usado': False,
            'confianza': 0
        }
        
        # ═══════════════════════════════════════════════════════
        # PASO 1: Buscar marca
        # ═══════════════════════════════════════════════════════
        marca_found = self._find_marca(full_text, marca_raw)
        
        if not marca_found:
            NORM_STATS.no_match += 1
            self._cache[cache_key] = result
            return result
        
        result['marca'] = marca_found['value']
        result['confianza'] += 40
        
        if marca_found.get('fuzzy'):
            result['fuzzy_usado'] = True
            NORM_STATS.fuzzy_marca += 1
        
        # ═══════════════════════════════════════════════════════
        # PASO 2: Buscar modelo
        # ═══════════════════════════════════════════════════════
        modelo_found = self._find_modelo(
            full_text, 
            marca_found['value'],
            modelo_raw
        )
        
        if not modelo_found:
            NORM_STATS.marca_only += 1
            result['normalizacion'] = 'solo_marca'
            self._cache[cache_key] = result
            return result
        
        result['modelo'] = modelo_found['value']
        result['confianza'] += 35
        
        if modelo_found.get('fuzzy'):
            result['fuzzy_usado'] = True
            NORM_STATS.fuzzy_modelo += 1
        
        # ═══════════════════════════════════════════════════════
        # PASO 3: Buscar versión
        # ═══════════════════════════════════════════════════════
        version_found = self._find_version(
            full_text,
            marca_found['value'],
            modelo_found['value'],
            version_raw
        )
        
        if version_found:
            result['version'] = version_found['value']
            result['confianza'] += 25
            result['normalizacion'] = 'completo'
            NORM_STATS.full_match += 1
            
            if version_found.get('fuzzy'):
                result['fuzzy_usado'] = True
                NORM_STATS.fuzzy_version += 1
        else:
            result['normalizacion'] = 'marca_modelo'
            NORM_STATS.marca_modelo += 1
        
        # Guardar en cache
        if len(self._cache) < 10000:
            self._cache[cache_key] = result
        
        return result
    
    def _find_marca(
        self,
        full_text: str,
        marca_raw: str
    ) -> Optional[Dict]:
        """Busca la marca en el texto."""
        
        # 1. Verificar si marca_raw (después de alias) existe directamente
        marca_norm = normalize_text(marca_raw)
        marca_resolved = self.catalog.resolve_alias(marca_norm)
        
        if self.catalog.brand_exists(marca_resolved):
            return {'value': marca_resolved, 'fuzzy': False}
        
        # 2. Buscar marcas del catálogo en el texto completo
        for brand in self.catalog.get_brands():
            if find_in_text(brand, full_text):
                return {'value': brand, 'fuzzy': False}
        
        # 3. Intentar fuzzy matching si está habilitado
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ and marca_norm:
            brands_list = self.catalog.get_brands()
            match = fuzzy_find_best(
                marca_norm,
                brands_list,
                CONFIG.fuzzy_threshold_marca
            )
            if match:
                return {'value': match[0], 'fuzzy': True, 'score': match[1]}
        
        return None
    
    def _find_modelo(
        self,
        full_text: str,
        marca: str,
        modelo_raw: str
    ) -> Optional[Dict]:
        """Busca el modelo en el texto."""
        
        modelos = self.catalog.get_models(marca)
        if not modelos:
            return None
        
        modelo_norm = normalize_text(modelo_raw)
        
        # 1. Verificar si modelo_raw existe directamente
        if modelo_norm in modelos:
            return {'value': modelo_norm, 'fuzzy': False}
        
        # 2. Buscar modelos en el texto (primero los más largos)
        for model in modelos:
            if find_in_text(model, full_text):
                return {'value': model, 'fuzzy': False}
        
        # 3. Fuzzy matching
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ and modelo_norm:
            match = fuzzy_find_best(
                modelo_norm,
                modelos,
                CONFIG.fuzzy_threshold_modelo
            )
            if match:
                return {'value': match[0], 'fuzzy': True, 'score': match[1]}
        
        return None
    
    def _find_version(
        self,
        full_text: str,
        marca: str,
        modelo: str,
        version_raw: str
    ) -> Optional[Dict]:
        """Busca la versión en el texto."""
        
        versiones = self.catalog.get_versions(marca, modelo)
        if not versiones:
            return None
        
        versiones_list = sorted(versiones, key=len, reverse=True)
        version_norm = normalize_text(version_raw)
        
        # 1. Verificar si version_raw existe directamente
        if version_norm in versiones:
            return {'value': version_norm, 'fuzzy': False}
        
        # 2. Buscar versiones en el texto
        for version in versiones_list:
            if find_in_text(version, full_text):
                return {'value': version, 'fuzzy': False}
        
        # 3. Fuzzy matching
        if CONFIG.enable_fuzzy and HAS_RAPIDFUZZ and version_norm:
            match = fuzzy_find_best(
                version_norm,
                versiones_list,
                CONFIG.fuzzy_threshold_version
            )
            if match:
                return {'value': match[0], 'fuzzy': True, 'score': match[1]}
        
        return None
    
    def clear_cache(self):
        """Limpia el cache de normalizaciones."""
        self._cache.clear()


# Instancia global del normalizador
NORMALIZER = VehicleNormalizer()


# ═══════════════════════════════════════════════════════════════
# 🚀 FUNCIONES DE INICIALIZACIÓN
# ═══════════════════════════════════════════════════════════════
def init_normalizer(
    catalog_path: str = None,
    aliases_path: str = None
) -> bool:
    """
    Inicializa el normalizador cargando catálogo y aliases.
    
    Args:
        catalog_path: Ruta al CSV del catálogo
        aliases_path: Ruta al JSON de aliases
    
    Returns:
        True si se cargó al menos el catálogo correctamente
    """
    global CATALOG, NORMALIZER, NORM_STATS
    
    # Reset stats
    NORM_STATS = NormalizationStats()
    
    # Usar paths por defecto si no se especifican
    catalog_path = catalog_path or CONFIG.catalog_path
    aliases_path = aliases_path or CONFIG.aliases_path
    
    # Cargar catálogo (requerido)
    if not CATALOG.load_from_csv(catalog_path):
        logger.warning("⚠️ No se pudo cargar el catálogo de vehículos")
        return False
    
    # Cargar aliases (opcional)
    CATALOG.load_aliases(aliases_path)
    
    # Reinicializar normalizador con el catálogo actualizado
    NORMALIZER = VehicleNormalizer(CATALOG)
    
    return True


def normalize_vehicle(
    titulo: str = "",
    descripcion: str = "",
    marca_raw: str = "",
    modelo_raw: str = "",
    version_raw: str = ""
) -> Dict:
    """
    Función de conveniencia para normalizar un vehículo.
    
    Returns:
        Dict con claves: marca, modelo, version (normalizados, en minúsculas)
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


# ═══════════════════════════════════════════════════════════════
# 🧪 PRUEBAS
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Configurar logging para pruebas
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("🧪 PRUEBA DEL NORMALIZADOR")
    print("=" * 60)
    
    # Intentar cargar catálogo
    if init_normalizer():
        # Casos de prueba
        test_cases = [
            {
                "titulo": "Fiat 128 Berlina 1980",
                "marca_raw": "Fiat",
                "modelo_raw": "Berlina",
                "version_raw": "Berlina"
            },
            {
                "titulo": "Volkswagen Amarok Highline 4x4",
                "descripcion": "Vendo Amarok Highline excelente estado",
                "marca_raw": "VW",
                "modelo_raw": "Amarok",
                "version_raw": ""
            },
            {
                "titulo": "Chevrolet Cruze LTZ 2019",
                "marca_raw": "Chev",
                "modelo_raw": "Cruze",
                "version_raw": "LTZ"
            },
        ]
        
        for i, test in enumerate(test_cases, 1):
            print(f"\n--- Caso {i} ---")
            print(f"Input: {test}")
            result = normalize_vehicle(**test)
            print(f"Output: marca={result['marca']}, modelo={result['modelo']}, version={result['version']}")
            print(f"Normalización: {result['normalizacion']}, Fuzzy: {result['fuzzy_usado']}")
        
        print(f"\n📊 Estadísticas: {NORM_STATS.summary()}")
    else:
        print("❌ No se pudo inicializar el normalizador")
        print("   Asegúrate de tener el archivo config/vehicles_catalog.csv")
