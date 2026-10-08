"""Dependency-free NBT writer for legacy Java 1.12.2 chunk format."""
try:
    from .nbt_tools import Tag,BYTE,SHORT,INT,LONG,BYTE_ARRAY,LIST,COMPOUND,INT_ARRAY
except ImportError:
    from nbt_tools import Tag,BYTE,SHORT,INT,LONG,BYTE_ARRAY,LIST,COMPOUND,INT_ARRAY

def _compound_list_items(items):
    # nbt_tools represents TAG_List<Compound> items as raw lists of Tag objects.
    # The writer needs each list element wrapped as an anonymous Compound Tag.
    out=[]
    for item in (items or []):
        if isinstance(item, Tag):
            out.append(item)
        elif isinstance(item, list):
            out.append(Tag(COMPOUND, '', item))
        else:
            raise TypeError(f'Unsupported NBT compound-list item: {type(item).__name__}')
    return out

def chunk_nbt(chunk_x,chunk_z,last_update,inhabited,height_map,biomes,sections,entities=None,tile_entities=None,tile_ticks=None):
    sec_tags=[]
    for s in sections:
        y,blocks,meta,sky,block_light=s
        sec_tags.append([Tag(BYTE,'Y',y),Tag(BYTE_ARRAY,'Blocks',bytes(blocks)),Tag(BYTE_ARRAY,'Data',bytes(meta)),Tag(BYTE_ARRAY,'SkyLight',bytes(sky)),Tag(BYTE_ARRAY,'BlockLight',bytes(block_light))])
    level=Tag(COMPOUND,'Level',[
        Tag(INT,'xPos',chunk_x),Tag(INT,'zPos',chunk_z),Tag(LONG,'LastUpdate',last_update),Tag(LONG,'InhabitedTime',inhabited),
        Tag(BYTE,'TerrainPopulated',1),Tag(BYTE,'LightPopulated',1),Tag(INT_ARRAY,'HeightMap',[int(x) for x in height_map]),Tag(BYTE_ARRAY,'Biomes',bytes(biomes)),
        Tag(LIST,'Sections',(COMPOUND,sec_tags)),
        Tag(LIST,'Entities',(COMPOUND,entities or [])),
        Tag(LIST,'TileEntities',(COMPOUND,tile_entities or []))])
    if tile_ticks is not None:
        level.value.append(tile_ticks)
    return Tag(COMPOUND,'',[Tag(INT,'DataVersion',1343),level])
