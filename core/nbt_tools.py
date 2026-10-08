"""Small dependency-free NBT reader/writer used by PS3ToPC.
Supports all standard NBT tag types and preserves compound/list structure.
"""
import struct, io

BYTE=1; SHORT=2; INT=3; LONG=4; FLOAT=5; DOUBLE=6; BYTE_ARRAY=7; STRING=8; LIST=9; COMPOUND=10; INT_ARRAY=11; LONG_ARRAY=12

class Tag:
    __slots__=('type','name','value')
    def __init__(self,t,name='',value=None): self.type=t; self.name=name; self.value=value

def _read_string(b,p):
    if p+2>len(b): raise ValueError('truncated NBT string length')
    n=struct.unpack_from('>H',b,p)[0]; p+=2
    if p+n>len(b): raise ValueError('truncated NBT string')
    return bytes(b[p:p+n]).decode('utf-8','replace'),p+n

def _read_payload(b,p,t):
    if t==BYTE:return struct.unpack_from('>b',b,p)[0],p+1
    if t==SHORT:return struct.unpack_from('>h',b,p)[0],p+2
    if t==INT:return struct.unpack_from('>i',b,p)[0],p+4
    if t==LONG:return struct.unpack_from('>q',b,p)[0],p+8
    if t==FLOAT:return struct.unpack_from('>f',b,p)[0],p+4
    if t==DOUBLE:return struct.unpack_from('>d',b,p)[0],p+8
    if t==BYTE_ARRAY:
        n=struct.unpack_from('>i',b,p)[0]; p+=4
        if n<0 or p+n>len(b): raise ValueError('invalid byte array')
        return bytes(b[p:p+n]),p+n
    if t==STRING:return _read_string(b,p)
    if t==LIST:
        if p+5>len(b): raise ValueError('truncated list')
        et=b[p]; n=struct.unpack_from('>i',b,p+1)[0]; p+=5
        if n<0: raise ValueError('negative list length')
        items=[]
        for _ in range(n):
            v,p=_read_payload(b,p,et); items.append(v)
        return (et,items),p
    if t==COMPOUND:
        tags=[]
        while True:
            if p>=len(b): raise ValueError('unterminated compound')
            tt=b[p]; p+=1
            if tt==0: break
            name,p=_read_string(b,p); v,p=_read_payload(b,p,tt); tags.append(Tag(tt,name,v))
        return tags,p
    if t==INT_ARRAY:
        n=struct.unpack_from('>i',b,p)[0];p+=4
        if n<0 or p+4*n>len(b): raise ValueError('invalid int array')
        return list(struct.unpack_from('>'+('i'*n),b,p)) if n else [],p+4*n
    if t==LONG_ARRAY:
        n=struct.unpack_from('>i',b,p)[0];p+=4
        if n<0 or p+8*n>len(b): raise ValueError('invalid long array')
        return list(struct.unpack_from('>'+('q'*n),b,p)) if n else [],p+8*n
    raise ValueError(f'unknown NBT tag type {t}')

def loads(data):
    b=memoryview(data); p=0
    if not b: raise ValueError('empty NBT')
    t=b[p];p+=1
    if t!=COMPOUND: raise ValueError('NBT root is not a compound')
    name,p=_read_string(b,p); value,p=_read_payload(b,p,COMPOUND)
    # Some LCE player/data files contain a legacy trailer after the root NBT.
    # The Java world only needs the first complete root compound.
    return Tag(COMPOUND,name,value)

def _write_string(s):
    b=str(s).encode('utf-8');
    if len(b)>65535: raise ValueError('NBT string too long')
    return struct.pack('>H',len(b))+b

def _write_payload(t,v):
    if t==BYTE:return struct.pack('>b',int(v))
    if t==SHORT:return struct.pack('>h',int(v))
    if t==INT:return struct.pack('>i',int(v))
    if t==LONG:return struct.pack('>q',int(v))
    if t==FLOAT:return struct.pack('>f',float(v))
    if t==DOUBLE:return struct.pack('>d',float(v))
    if t==BYTE_ARRAY:
        v=bytes(v); return struct.pack('>i',len(v))+v
    if t==STRING:return _write_string(v)
    if t==LIST:
        et,items=v
        if et==COMPOUND:
            # Parser stores TAG_List<Compound> elements as raw compound lists.
            # Accept both raw lists and anonymous Compound Tags.
            payloads=[]
            for x in items:
                if isinstance(x, Tag):
                    x = x.value
                payloads.append(_write_payload(COMPOUND, x))
            return bytes([et])+struct.pack('>i',len(items))+b''.join(payloads)
        return bytes([et])+struct.pack('>i',len(items))+b''.join(_write_payload(et,x) for x in items)
    if t==COMPOUND:return b''.join(bytes([x.type])+_write_string(x.name)+_write_payload(x.type,x.value) for x in v)+b'\x00'
    if t==INT_ARRAY:return struct.pack('>i',len(v))+b''.join(struct.pack('>i',int(x)) for x in v)
    if t==LONG_ARRAY:return struct.pack('>i',len(v))+b''.join(struct.pack('>q',int(x)) for x in v)
    raise ValueError(t)

def dumps(root):
    if root.type!=COMPOUND: raise ValueError('root must be compound')
    return bytes([COMPOUND])+_write_string(root.name)+_write_payload(COMPOUND,root.value)

def child(comp,name):
    if comp.type!=COMPOUND: return None
    return next((x for x in comp.value if x.name==name),None)

def set_child(comp,tag):
    for i,x in enumerate(comp.value):
        if x.name==tag.name: comp.value[i]=tag; return
    comp.value.append(tag)

def remove_child(comp,name): comp.value[:]=[x for x in comp.value if x.name!=name]
