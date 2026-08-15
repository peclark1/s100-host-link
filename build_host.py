#!/usr/bin/env python3
"""Tiny two-pass Intel 8080 assembler used to build HOST.COM.

It intentionally implements only the directives/instructions used by HOST.ASM.
This keeps the build self-contained on a modern Linux machine.
"""
from __future__ import annotations
import ast
import re
import sys
from pathlib import Path

REG = {'B':0,'C':1,'D':2,'E':3,'H':4,'L':5,'M':6,'A':7}
RP = {'B':0,'D':1,'H':2,'SP':3}
PUSHPOP = {'B':0,'D':1,'H':2,'PSW':3}

class AsmError(Exception): pass

def strip_comment(line:str)->str:
    out=[]; quote=None
    for ch in line:
        if quote:
            out.append(ch)
            if ch==quote: quote=None
        elif ch in "'\"":
            quote=ch; out.append(ch)
        elif ch==';': break
        else: out.append(ch)
    return ''.join(out).strip()

def split_args(s:str):
    args=[]; cur=[]; quote=None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch==quote: quote=None
        elif ch in "'\"": quote=ch; cur.append(ch)
        elif ch==',': args.append(''.join(cur).strip()); cur=[]
        else: cur.append(ch)
    if cur or s.strip(): args.append(''.join(cur).strip())
    return args

def normalize_expr(expr:str)->str:
    expr=expr.strip()
    expr=re.sub(r'\b([0-9A-Fa-f]+)[Hh]\b', lambda m:str(int(m.group(1),16)), expr)
    expr=re.sub(r'\b([01]+)[Bb]\b', lambda m:str(int(m.group(1),2)), expr)
    return expr

def eval_expr(expr:str, syms:dict[str,int])->int:
    expr=normalize_expr(expr)
    # Replace symbols with numeric values, longest first.
    for name in sorted(syms, key=len, reverse=True):
        expr=re.sub(rf'\b{re.escape(name)}\b', str(syms[name]), expr, flags=re.I)
    try:
        node=ast.parse(expr, mode='eval')
    except SyntaxError as e: raise AsmError(f"bad expression {expr!r}") from e
    allowed=(ast.Expression,ast.Constant,ast.UnaryOp,ast.BinOp,ast.Add,ast.Sub,ast.Mult,
             ast.Div,ast.FloorDiv,ast.Mod,ast.LShift,ast.RShift,ast.BitOr,ast.BitAnd,
             ast.BitXor,ast.Invert,ast.USub,ast.UAdd,ast.Pow)
    for n in ast.walk(node):
        if not isinstance(n, allowed): raise AsmError(f"unsupported expression {expr!r}")
    try:
        value=eval(compile(node,'<expr>','eval'),{'__builtins__':{}},{})
        if isinstance(value,str):
            if len(value)!=1: raise ValueError('character expression must be length 1')
            return ord(value)
        return int(value)
    except Exception as e: raise AsmError(f"unresolved expression {expr!r}") from e

def parse_line(line:str):
    line=strip_comment(line)
    if not line: return None,None,None
    label=None
    if ':' in line:
        first,rest=line.split(':',1)
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', first.strip()):
            label=first.strip().upper(); line=rest.strip()
            if not line: return label,None,None
    parts=line.split(None,2)
    if len(parts)>=2 and parts[1].upper()=='EQU' and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',parts[0]):
        return parts[0].upper(),'EQU',parts[2] if len(parts)>2 else ''
    op=parts[0].upper(); args=parts[1] if len(parts)>1 else ''
    if len(parts)>2: args += ' ' + parts[2]
    return label,op,args.strip()

def inst_size(op,args):
    op=op.upper()
    if op in ('ORG','EQU'): return 0
    if op=='DB':
        n=0
        for a in split_args(args):
            a=a.strip()
            if len(a)>=2 and a[0] in "'\"" and a[-1]==a[0]: n += len(ast.literal_eval(a))
            else: n += 1
        return n
    if op=='DS': return int(normalize_expr(args),0) if re.fullmatch(r'\d+',normalize_expr(args)) else 0
    if op in ('LXI','JMP','JNZ','JZ','JNC','JC','CALL','LDA','STA','LHLD','SHLD'): return 3
    if op in ('MVI','ADI','ACI','SUI','SBI','ANI','XRI','ORI','CPI','IN','OUT'): return 2
    return 1

def encode(op,args,syms):
    op=op.upper(); A=[x.upper() for x in split_args(args)]
    e=lambda x:eval_expr(x,syms)
    def w(v): return [v&255,(v>>8)&255]
    if op=='DB':
        out=[]
        for raw in split_args(args):
            raw=raw.strip()
            if len(raw)>=2 and raw[0] in "'\"" and raw[-1]==raw[0]:
                val=ast.literal_eval(raw)
                out.extend(val.encode('latin1') if isinstance(val,str) else val)
            else: out.append(e(raw)&255)
        return out
    if op=='DS': return [0]*(e(args))
    if op in ('ORG','EQU'): return []
    one={
      'NOP':0x00,'RLC':0x07,'RRC':0x0F,'RAL':0x17,'RAR':0x1F,'CMA':0x2F,'STC':0x37,'CMC':0x3F,
      'HLT':0x76,'RNZ':0xC0,'RZ':0xC8,'RET':0xC9,'RNC':0xD0,'RC':0xD8,'RPO':0xE0,'RPE':0xE8,
      'XTHL':0xE3,'PCHL':0xE9,'XCHG':0xEB,'RP':0xF0,'RM':0xF8,'DI':0xF3,'SPHL':0xF9,'EI':0xFB,
    }
    if op in one: return [one[op]]
    if op=='MOV': return [0x40+REG[A[0]]*8+REG[A[1]]]
    if op=='MVI': return [0x06+REG[A[0]]*8, e(split_args(args)[1])&255]
    if op=='LXI': return [0x01+RP[A[0]]*0x10]+w(e(split_args(args)[1]))
    if op=='INX': return [0x03+RP[A[0]]*0x10]
    if op=='DCX': return [0x0B+RP[A[0]]*0x10]
    if op=='DAD': return [0x09+RP[A[0]]*0x10]
    if op=='INR': return [0x04+REG[A[0]]*8]
    if op=='DCR': return [0x05+REG[A[0]]*8]
    if op=='LDAX': return [0x0A + ({'B':0,'D':1}[A[0]])*0x10]
    if op=='STAX': return [0x02 + ({'B':0,'D':1}[A[0]])*0x10]
    alu={'ADD':0x80,'ADC':0x88,'SUB':0x90,'SBB':0x98,'ANA':0xA0,'XRA':0xA8,'ORA':0xB0,'CMP':0xB8}
    if op in alu: return [alu[op]+REG[A[0]]]
    imm={'ADI':0xC6,'ACI':0xCE,'SUI':0xD6,'SBI':0xDE,'ANI':0xE6,'XRI':0xEE,'ORI':0xF6,'CPI':0xFE}
    if op in imm: return [imm[op], e(args)&255]
    jumps={'JNZ':0xC2,'JMP':0xC3,'JZ':0xCA,'JNC':0xD2,'JC':0xDA,'JPO':0xE2,'JPE':0xEA,'JP':0xF2,'JM':0xFA,
           'CALL':0xCD}
    if op in jumps: return [jumps[op]]+w(e(args))
    if op=='PUSH': return [0xC5+PUSHPOP[A[0]]*0x10]
    if op=='POP': return [0xC1+PUSHPOP[A[0]]*0x10]
    if op=='IN': return [0xDB,e(args)&255]
    if op=='OUT': return [0xD3,e(args)&255]
    if op=='LDA': return [0x3A]+w(e(args))
    if op=='STA': return [0x32]+w(e(args))
    if op=='LHLD': return [0x2A]+w(e(args))
    if op=='SHLD': return [0x22]+w(e(args))
    raise AsmError(f"unsupported opcode {op} {args}")

def assemble(text:str):
    lines=text.splitlines(); syms={}; pc=0; origin=None
    parsed=[]
    # pass 1
    for ln,line in enumerate(lines,1):
        label,op,args=parse_line(line)
        if label and op=='EQU':
            syms[label]=eval_expr(args,syms); parsed.append((ln,label,op,args,pc)); continue
        if label:
            if label in syms: raise AsmError(f"line {ln}: duplicate {label}")
            syms[label]=pc
        if not op: parsed.append((ln,label,op,args,pc)); continue
        if op=='ORG':
            pc=eval_expr(args,syms)
            if origin is None: origin=pc
        elif op=='DS': pc += eval_expr(args,syms)
        else: pc += inst_size(op,args)
        parsed.append((ln,label,op,args,pc))
    if origin is None: origin=0
    # pass 2
    pc=0; out=bytearray(); current_origin=None
    for ln,line in enumerate(lines,1):
        label,op,args=parse_line(line)
        if not op or op=='EQU': continue
        if op=='ORG':
            newpc=eval_expr(args,syms)
            if current_origin is None: current_origin=newpc; pc=newpc
            else:
                if newpc<pc: raise AsmError(f"line {ln}: backward ORG")
                out.extend(b'\0'*(newpc-pc)); pc=newpc
            continue
        try: b=encode(op,args,syms)
        except AsmError as e: raise AsmError(f"line {ln}: {e}") from e
        out.extend(b); pc += len(b)
    return origin,bytes(out),syms

def main():
    src=Path(sys.argv[1] if len(sys.argv)>1 else Path(__file__).with_name('HOST.ASM'))
    dst=Path(sys.argv[2] if len(sys.argv)>2 else src.with_suffix('.COM'))
    origin,data,syms=assemble(src.read_text(encoding='utf-8'))
    if origin!=0x100: raise SystemExit(f"Expected ORG 0100H, got {origin:04X}H")
    dst.write_bytes(data)
    print(f"Built {dst}: {len(data)} bytes, ORG {origin:04X}H")
    print(f"Entry 0100H; end {origin+len(data)-1:04X}H")
if __name__=='__main__': main()
